"""Разбор платежа «оплачен, не применён» командой (сценарий «Разобрать платёж», 4.21–4.24).

Действия: применить повторно, применить созданием новой подписки, отметить решённым
вручную (с комментарием), привязать неизвестный платёж к клиенту. Вернуть деньги —
блок 10. Каждое действие — в журнале с участником команды (4.25); после решения
клиент получает уведомление о результате (4.24).
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.journal import Actor, JsonValue
from remnabay.messaging import send_to_client
from remnabay.payments._apply import apply_payment, enqueue_apply, journal_payment
from remnabay.queue import (
    QueueAttempt,
    TaskRegistry,
    TaskStatus,
    attempts_of,
    resolve_failed,
    retry_failed,
    tasks_for,
)

# Для отметки «решено вручную» у задачи применения нет своих последствий
_TASKS = TaskRegistry((apply_payment,))


class PaymentActionError(Exception):
    """Действие недоступно для платежа в его состоянии."""


async def _locked(session: AsyncSession, payment_id: int) -> Payment:
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        raise PaymentActionError(f"Платёж {payment_id} не найден")
    return payment


async def _not_applied(session: AsyncSession, payment_id: int, *, bound: bool) -> Payment:
    payment = await _locked(session, payment_id)
    if payment.state != PaymentState.PAID_NOT_APPLIED:
        raise PaymentActionError("Платёж не в состоянии «оплачен — не применён»")
    if bound and payment.client_id is None:
        raise PaymentActionError("Неизвестный платёж сначала привязывают к клиенту")
    return payment


async def _failed_apply_task(session: AsyncSession, payment_id: int) -> int | None:
    found = await tasks_for(
        session, apply_payment.name, {"payment_id": payment_id}, status=TaskStatus.FAILED
    )
    return found[-1] if found else None


async def _apply_again(session: AsyncSession, payment: Payment, actor: Actor) -> None:
    """Платёж снова «оплачен» и применяется: проваленная задача — новым кругом попыток
    (она держит очередь операций подписки, 4.27), иначе — новая задача."""
    payment.state = PaymentState.PAID
    task_id = await _failed_apply_task(session, payment.id)
    if task_id is not None:
        await retry_failed(session, task_id, actor=actor)
    else:
        await enqueue_apply(session, payment)


async def retry_payment(session: AsyncSession, payment_id: int, *, member_id: int) -> None:
    """«Применить повторно» — если причина устранена (например, панель снова доступна)."""
    actor = Actor.team_member(member_id)
    payment = await _not_applied(session, payment_id, bound=True)
    await _apply_again(session, payment, actor)
    await journal_payment(session, payment, actor, "payment.retried")


async def apply_as_new_subscription(
    session: AsyncSession, payment_id: int, *, member_id: int
) -> None:
    """«Применить иначе» — создать новую подписку, например если пользователя удалили
    в панели. Условия платежа (тариф, цена) — те же, что зафиксированы при создании."""
    actor = Actor.team_member(member_id)
    payment = await _not_applied(session, payment_id, bound=True)
    details: dict[str, JsonValue] = {"previous_subscription_id": payment.subscription_id}
    # Проваленная задача держит очередь прежней подписки (4.27), а платёж её больше не
    # касается: задача закрывается, применение ставится заново — среди покупок клиента
    task_id = await _failed_apply_task(session, payment.id)
    if task_id is not None:
        comment = "Платёж применяется созданием новой подписки"
        await resolve_failed(session, _TASKS, task_id, actor=actor, comment=comment)
    payment.purpose = PaymentPurpose.PURCHASE
    payment.subscription_id = None
    payment.state = PaymentState.PAID
    await enqueue_apply(session, payment)
    await journal_payment(session, payment, actor, "payment.applying_as_new", details=details)


async def resolve_payment_manually(
    session: AsyncSession, payment_id: int, *, member_id: int, comment: str
) -> None:
    """«Отметить решённым вручную» (4.22): комментарий обязателен и виден только команде;
    клиент получает нейтральное уведомление (4.24)."""
    comment = comment.strip()
    if not comment:
        raise PaymentActionError("Нужен комментарий: что сделано вручную")
    actor = Actor.team_member(member_id)
    payment = await _not_applied(session, payment_id, bound=False)
    payment.state = PaymentState.RESOLVED_MANUALLY
    task_id = await _failed_apply_task(session, payment.id)
    if task_id is not None:
        await resolve_failed(session, _TASKS, task_id, actor=actor, comment=comment)
    await journal_payment(
        session, payment, actor, "payment.resolved_manually", details={"comment": comment}
    )
    if payment.client_id is not None:
        await send_to_client(session, payment.client_id, "event.payment_resolved.manual")


async def bind_unknown_payment(
    session: AsyncSession,
    payment_id: int,
    *,
    member_id: int,
    client_id: int,
    purpose: PaymentPurpose,
    tariff_id: int,
    tariff_snapshot: dict[str, JsonValue],
    subscription_id: int | None = None,
) -> None:
    """Привязать неизвестный платёж к клиенту и выбрать, что применить (4.23): покупку
    или продление конкретной подписки клиента, с тарифом. Условия тарифа фиксирует
    тот, кто вызывает (блок 3)."""
    actor = Actor.team_member(member_id)
    payment = await _not_applied(session, payment_id, bound=False)
    if not payment.is_unknown or payment.client_id is not None:
        raise PaymentActionError("Привязать можно только неизвестный платёж")
    if await session.get(Client, client_id) is None:
        raise PaymentActionError(f"Клиент {client_id} не найден")
    if purpose == PaymentPurpose.RENEWAL:
        subscription = await session.get(Subscription, subscription_id or 0)
        if subscription is None or subscription.client_id != client_id:
            raise PaymentActionError("Продлить можно только подписку этого клиента")
    elif subscription_id is not None:
        raise PaymentActionError("Покупка создаёт новую подписку — подписку не выбирают")
    payment.client_id = client_id
    payment.purpose = purpose
    payment.subscription_id = subscription_id
    payment.tariff_id = tariff_id
    payment.tariff_snapshot = tariff_snapshot
    await _apply_again(session, payment, actor)
    await journal_payment(
        session,
        payment,
        actor,
        "payment.bound",
        details={"client_id": client_id, "purpose": purpose.value},
    )


@dataclass(frozen=True)
class PaymentCard:
    """Карточка платежа (4.21): клиент, что должно было примениться, попытки и ошибка."""

    payment: Payment
    client: Client | None
    subscription: Subscription | None
    attempts: list[QueueAttempt]

    @property
    def last_error(self) -> str | None:
        errors = [attempt.error for attempt in self.attempts if attempt.error]
        return errors[-1] if errors else None


async def payment_card(session: AsyncSession, payment_id: int) -> PaymentCard:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise PaymentActionError(f"Платёж {payment_id} не найден")
    attempts: list[QueueAttempt] = []
    for task_id in await tasks_for(session, apply_payment.name, {"payment_id": payment_id}):
        attempts.extend(await attempts_of(session, task_id))
    client = await session.get(Client, payment.client_id) if payment.client_id else None
    subscription = (
        await session.get(Subscription, payment.subscription_id)
        if payment.subscription_id
        else None
    )
    return PaymentCard(payment=payment, client=client, subscription=subscription, attempts=attempts)


async def payments_needing_attention(session: AsyncSession) -> list[Payment]:
    """Платежи «оплачен — не применён», включая неизвестные (4.20, 4.23), от старых к новым."""
    result = await session.scalars(
        select(Payment)
        .where(Payment.state == PaymentState.PAID_NOT_APPLIED)
        .order_by(Payment.paid_at, Payment.id)
    )
    return list(result)
