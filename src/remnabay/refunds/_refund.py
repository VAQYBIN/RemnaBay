"""Возврат вручную (сценарий «Вернуть деньги», 10.8–10.14, 10.16; решения 0033, 0065).

- Вернуть можно платёж «применён», «оплачен — не применён» (в том числе неизвестный)
  и «решён вручную»; платёж с нулевой суммой — нельзя (10.16). Возврат по платежу
  один (10.12). Возвращает только владелец — права проверяет вызывающий.
- Сумма по умолчанию — неиспользованная стоимость по отрезкам (10.8); у неприменённого,
  неизвестного и «решён вручную» — вся оплата (10.14, 10.16). Больше заплаченного
  вернуть нельзя.
- Владелец возвращает деньги сам: платёж сразу «возвращён» или «частично возвращён»
  (10.11). Срок подписки сокращается на неиспользованную часть платежа независимо от
  суммы (10.9) — операцией очереди в порядке операций подписки (4.14, 4.17). Её нельзя
  отменить: деньги уже вернули (4.31). Клиент получает сообщение о возврате, когда
  дата применена или операция отмечена решённой вручную (10.11).
- Возврат по неприменённому платежу закрывает его проваленное применение: следующие
  операции подписки идут дальше (10.14, 4.27).
- Если после возврата подписка заканчивается сейчас, у неё нет напоминаний и «Подписка
  истекла» — только сообщение о возврате (10.13).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.payments import Payment, PaymentState, Refund, RefundMethod, RefundRequest
from remnabay.domain.subscriptions import Subscription, TermSegment, operations_key
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_BUTTON, send_to_client
from remnabay.panel import PanelError, UpdateUser
from remnabay.panel_sync import Source, accept_user, enqueue_reconcile, term_end
from remnabay.payments import payment_subject, release_failed_apply
from remnabay.queue import TaskContext, task
from remnabay.refunds._requests import close_by_refund, open_requests, request_closed_by
from remnabay.refunds._term import cut_term, unused_time, unused_value
from remnabay.reminders import silence_after_refund

SUBSCRIPTION_SUBJECT = "subscription"
REFUNDABLE = frozenset(
    {PaymentState.APPLIED, PaymentState.PAID_NOT_APPLIED, PaymentState.RESOLVED_MANUALLY}
)
_CENT = Decimal("0.01")
# Панель хранит дату с точностью до миллисекунд: даты ближе секунды — одна и та же
_SAME_MOMENT = timedelta(seconds=1)
# Дату окончания в прошлом панель не принимает: подписка, которую возврат закончил,
# действует в панели до ближайшего момента, который та примет
_PANEL_LEAD = timedelta(minutes=1)


class RefundError(Exception):
    """Вернуть нельзя; текст показывается владельцу."""


def paid_amount(payment: Payment) -> Decimal:
    """Сколько заплачено по платежу — больше вернуть нельзя (10.8). Сумма, которую
    подтвердил провайдер: у неизвестного платежа и платежа с несовпавшей суммой это и
    есть «вся оплата» (10.14)."""
    if payment.paid_amount is not None and payment.paid_currency == payment.currency:
        return payment.paid_amount
    return payment.amount


def check_refundable(payment: Payment) -> None:
    if payment.state in (PaymentState.REFUNDED, PaymentState.PARTIALLY_REFUNDED):
        raise RefundError("По платежу уже был возврат — повторный недоступен")
    if payment.state not in REFUNDABLE:
        raise RefundError("Вернуть можно применённый, неприменённый или решённый вручную платёж")
    if paid_amount(payment) <= 0:
        raise RefundError("Платёж с нулевой суммой вернуть нельзя: возвращать нечего")


def refundable(payment: Payment) -> bool:
    try:
        check_refundable(payment)
    except RefundError:
        return False
    return True


@dataclass(frozen=True)
class TermChange:
    """Как изменится срок подписки (10.9, 10.10)."""

    subscription: Subscription
    current_end: datetime
    new_end: datetime
    # После возврата подписка заканчивается сейчас (10.13)
    ends_now: bool


async def _segments(session: AsyncSession, subscription_id: int) -> list[TermSegment]:
    result = await session.scalars(
        select(TermSegment)
        .where(TermSegment.subscription_id == subscription_id)
        .order_by(TermSegment.starts_at, TermSegment.id)
    )
    return list(result)


async def _subscription(session: AsyncSession, payment: Payment) -> Subscription | None:
    """Подписка платежа, которую видит клиент."""
    if payment.subscription_id is None:
        return None
    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None or subscription.deleted_at is not None:
        return None
    return subscription


async def term_change(session: AsyncSession, payment: Payment, at: datetime) -> TermChange | None:
    """Новый срок, если возврат его меняет. Срок меняется только у применённого
    магазином платежа: «решён вручную» применяла команда, и магазин его не трогает (10.16)."""
    if payment.state != PaymentState.APPLIED:
        return None
    subscription = await _subscription(session, payment)
    if subscription is None:
        return None
    removed = unused_time(await _segments(session, subscription.id), payment.id, at)
    current_end = await term_end(session, subscription)
    if not removed or current_end is None:
        return None
    new_end = max(current_end - removed, at)
    return TermChange(
        subscription=subscription,
        current_end=current_end,
        new_end=new_end,
        ends_now=new_end - at < _SAME_MOMENT,
    )


@dataclass(frozen=True)
class RefundQuote:
    """Экран возврата (А5, 10.8, 10.10): предложенная сумма, предел, новый срок и
    открытые запросы клиента."""

    payment: Payment
    paid: Decimal
    suggested: Decimal
    subscription: Subscription | None
    change: TermChange | None
    requests: list[RefundRequest]


async def refund_quote(session: AsyncSession, payment_id: int, now: datetime) -> RefundQuote:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise RefundError(f"Платёж {payment_id} не найден")
    check_refundable(payment)
    paid = paid_amount(payment)
    suggested = paid
    if payment.state == PaymentState.APPLIED and payment.subscription_id is not None:
        segments = await _segments(session, payment.subscription_id)
        suggested = min(unused_value(segments, payment.id, now), paid)
    requests = await open_requests(session, payment.client_id) if payment.client_id else []
    return RefundQuote(
        payment=payment,
        paid=paid,
        suggested=suggested,
        subscription=await _subscription(session, payment),
        change=await term_change(session, payment, now),
        requests=requests,
    )


def _check_amount(amount: Decimal, paid: Decimal) -> None:
    if amount != amount.quantize(_CENT):
        raise RefundError("Сумма — с точностью до копейки")
    if amount <= 0:
        raise RefundError("Сумма возврата должна быть больше нуля")
    if amount > paid:
        raise RefundError("Нельзя вернуть больше, чем заплачено по платежу")


async def refund_payment(
    session: AsyncSession,
    payment_id: int,
    *,
    amount: Decimal,
    member_id: int,
    now: datetime,
    request_id: int | None = None,
) -> Refund:
    """Владелец вернул деньги сам и отмечает возврат выполненным вручную (10.11)."""
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        raise RefundError(f"Платёж {payment_id} не найден")
    check_refundable(payment)
    paid = paid_amount(payment)
    _check_amount(amount, paid)
    request = await request_closed_by(session, payment.client_id, request_id)
    previous = payment.state
    change = await term_change(session, payment, now)
    refund = Refund(
        payment_id=payment.id,
        amount=amount,
        method=RefundMethod.MONEY,
        performed_manually=True,
        performed_by_id=member_id,
        performed_at=now,
    )
    session.add(refund)
    await session.flush()
    payment.state = PaymentState.REFUNDED if amount == paid else PaymentState.PARTIALLY_REFUNDED
    details: dict[str, JsonValue] = {
        "state": payment.state.value,
        "previous": previous.value,
        "amount": str(amount),
        "refund_id": refund.id,
        "request_id": request.id if request else None,
        "manually": True,
    }
    if change is not None:
        details["new_end"] = change.new_end.isoformat()
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="payment.refunded",
        outcome=Outcome.SUCCESS,
        subject=payment_subject(payment.id),
        details=details,
    )
    if request is not None:
        await close_by_refund(session, request, refund, member_id=member_id)
    if previous == PaymentState.PAID_NOT_APPLIED:
        # 10.14: применять больше нечего — ожидание снимается с операций подписки
        await release_failed_apply(
            session, payment.id, member_id=member_id, comment="Деньги по платежу возвращены"
        )
        await _notify(session, payment, None, None)
        return refund
    if change is not None:
        await change_term.enqueue(
            session,
            TermArgs(
                refund_id=refund.id,
                payment_id=payment.id,
                subscription_id=change.subscription.id,
                refunded_at=now,
                expected_end=change.new_end,
                ends_now=change.ends_now,
            ),
            key=operations_key(change.subscription.id),
        )
        return refund
    # Срок не меняется: платёж «решён вручную» или полностью использован
    subscription = await _subscription(session, payment)
    end = subscription.expires_at if subscription is not None else None
    await _notify(session, payment, subscription, end)
    return refund


async def _notify(
    session: AsyncSession,
    payment: Payment,
    subscription: Subscription | None,
    end: datetime | None,
    *,
    ended: bool = False,
) -> None:
    """Сообщение о возврате (10.11): С11 с новой датой окончания или «подписка
    завершена» (10.13); у платежа без подписки — «Мы разобрались с вашим платежом:
    деньги возвращены» (С12). `ended` — возврат закончил подписку, даже если панель
    закроет её через минуту."""
    if payment.client_id is None:
        return
    if subscription is None or end is None:
        await send_to_client(
            session,
            payment.client_id,
            "event.payment_resolved.refunded",
            buttons=[MAIN_MENU_BUTTON],
        )
        return
    payment_date = payment.paid_at or payment.created_at
    variables = {"subscription_name": subscription.name}
    if ended or end <= datetime.now(UTC):
        await send_to_client(
            session,
            payment.client_id,
            "event.refunded.ended",
            variables=variables,
            dates={"payment_date": payment_date},
            buttons=[MAIN_MENU_BUTTON],
        )
        return
    await send_to_client(
        session,
        payment.client_id,
        "event.refunded",
        variables=variables,
        dates={"payment_date": payment_date, "end_date": end},
        buttons=[MAIN_MENU_BUTTON],
    )


# --- Смена даты при возврате (10.9, 10.11, 4.31) ---


class TermArgs(BaseModel):
    refund_id: int
    payment_id: int
    subscription_id: int
    # Момент возврата: неиспользованная часть считается на него
    refunded_at: datetime
    # Срок, который ожидался в момент возврата, — для сообщения клиенту, если команда
    # отметит операцию решённой вручную
    expected_end: datetime
    ends_now: bool


async def _journal_term(
    session: AsyncSession, subscription: Subscription, args: TermArgs, end: datetime
) -> None:
    await record(
        session,
        actor=Actor.SYSTEM,
        action="subscription.term_cut_by_refund",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details={
            "payment_id": args.payment_id,
            "refund_id": args.refund_id,
            "expires_at": end.isoformat(),
        },
    )


@task("refunds.change_term", TermArgs, cancellable=False)
async def change_term(context: TaskContext, args: TermArgs) -> None:
    """Срок подписки сокращается на неиспользованную часть возвращённого платежа (10.9).

    Перед изменением панели сверяется её состояние: не сократила ли срок прошлая
    попытка (4.15). Отрезки меняются после того, как панель приняла дату (4.34).
    Строка подписки блокируется до обращения к панели (4.17)."""
    session = context.session
    subscription = await session.get(Subscription, args.subscription_id, with_for_update=True)
    payment = await session.get(Payment, args.payment_id)
    if subscription is None or payment is None:
        return
    if subscription.deleted_at is not None:
        raise PanelError("Пользователя подписки удалили в панели — сменить дату некуда")
    panel = runtime.current().panel
    user = await panel.get_user(subscription.panel_user_id)
    if user is None:
        raise PanelError("Пользователя подписки удалили в панели — сменить дату некуда")
    at = args.refunded_at
    now = datetime.now(UTC)
    removed = unused_time(await _segments(session, subscription.id), payment.id, at)
    known_end = await term_end(session, subscription)
    applied_before = (
        bool(removed)
        and known_end is not None
        and _panel_has(user.expire_at, max(known_end - removed, at), now)
    )
    if not applied_before:
        # Магазин сначала узнаёт, что сейчас в панели, и только потом меняет её сам
        await accept_user(session, subscription, user)
        removed = unused_time(await _segments(session, subscription.id), payment.id, at)
        known_end = await term_end(session, subscription)
        if removed and known_end is not None:
            end = max(known_end - removed, at, now + _PANEL_LEAD)
            user = await panel.update_user(UpdateUser(id=user.id, expire_at=end))
    await cut_term(session, await _segments(session, subscription.id), payment.id, at)
    # Срок магазина — как в панели: минута, на которую панель продлила закончившуюся
    # подписку, остаётся отрезком без оплаты
    await accept_user(session, subscription, user)
    await _journal_term(session, subscription, args, user.expire_at)
    ended = user.expire_at <= now + _PANEL_LEAD
    if ended:
        await silence_after_refund(session, subscription, user.expire_at)
    await _notify(session, payment, subscription, user.expire_at, ended=ended)


def _panel_has(panel_end: datetime, wanted: datetime, now: datetime) -> bool:
    """Прошлая попытка уже поставила в панели срок после возврата (4.15): ту же дату,
    а если она в прошлом — ближайший момент, который панель приняла."""
    if abs(panel_end - wanted) < _SAME_MOMENT:
        return True
    return wanted < now + _PANEL_LEAD and wanted <= panel_end <= now + _PANEL_LEAD


@change_term.on_resolved
async def _term_resolved(session: AsyncSession, _task_id: int, args: TermArgs) -> None:
    """Команда сменила дату в панели сама и отметила операцию решённой (4.31): клиент
    получает сообщение о возврате (10.11), магазин сверяет подписку с панелью."""
    subscription = await session.get(Subscription, args.subscription_id, with_for_update=True)
    payment = await session.get(Payment, args.payment_id)
    if subscription is None or payment is None:
        return
    if args.ends_now:
        # Команда тоже не может поставить дату в прошлом — только ближайший момент
        await silence_after_refund(session, subscription, datetime.now(UTC) + _PANEL_LEAD)
    if subscription.deleted_at is None:
        await enqueue_reconcile(session, subscription.id, Source.SYNC, "refund")
    await _notify(
        session,
        payment,
        subscription if subscription.deleted_at is None else None,
        args.refunded_at if args.ends_now else args.expected_end,
        ended=args.ends_now,
    )
