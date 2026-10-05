"""Применение оплаченного платежа — операция очереди (4.11, 4.14, 4.19, 4.20).

Деньги пришли — услуга будет (0008): пока идут повторы, платёж «оплачен»; панель
недоступна — операция ждёт её и применяется после восстановления (4.11, 4.30);
попытки исчерпаны — «оплачен — не применён», и решает команда (4.20).

Что именно записать в панель и магазин при покупке и продлении, решает шаг
применения (`PaymentApplier`) — его даёт блок 3. Рама отвечает за состояния
платежа, повторы, сообщения и уведомления.
"""

from datetime import UTC, datetime, timedelta

from babel.numbers import format_currency
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.payments import Payment, PaymentState
from remnabay.domain.subscriptions import Subscription, operations_key
from remnabay.journal import Actor, ActorType, JournalEntry, JsonValue, Outcome, Subject, record
from remnabay.messaging import load_texts, notify_team, send_to_client
from remnabay.payments._applier import Applied
from remnabay.queue import TaskContext, task
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting, shop_time_zone

PAYMENT_SUBJECT = "payment"
# Если за это время платёж не применён, клиент получает «Оплата получена, подписка
# создаётся» (4.19): обычно применение укладывается в несколько секунд
PROCESSING_NOTICE_DELAY = timedelta(seconds=10)


def payment_subject(payment_id: int) -> Subject:
    return Subject(PAYMENT_SUBJECT, payment_id)


class PaymentArgs(BaseModel):
    payment_id: int


async def journal_payment(
    session: AsyncSession,
    payment: Payment,
    actor: Actor,
    action: str,
    *,
    outcome: Outcome = Outcome.SUCCESS,
    details: dict[str, JsonValue] | None = None,
) -> None:
    """Переход платежа — в журнал (3.34, 4.25)."""
    await record(
        session,
        actor=actor,
        action=action,
        outcome=outcome,
        subject=payment_subject(payment.id),
        details={"state": payment.state.value, **(details or {})},
    )


async def money(session: AsyncSession, payment: Payment) -> str:
    """Сумма платежа для текста: «199,00 ₽»."""
    language = await get_setting(session, SHOP_LANGUAGE)
    return format_currency(payment.amount, payment.currency, locale=language)


def _apply_key(payment: Payment) -> str | None:
    """Продление — после операций подписки (4.17); покупки клиента — по очереди, чтобы
    вторая оплата той же покупки видела подписку, созданную первой (3.11)."""
    if payment.subscription_id is not None:
        return operations_key(payment.subscription_id)
    if payment.client_id is not None:
        return f"client:{payment.client_id}:purchase"
    return None


async def enqueue_apply(session: AsyncSession, payment: Payment) -> None:
    """Ставит применение оплаченного платежа и, на случай задержки, сообщение 4.19."""
    args = PaymentArgs(payment_id=payment.id)
    await apply_payment.enqueue(session, args, key=_apply_key(payment))
    await processing_notice.enqueue(session, args, delay=PROCESSING_NOTICE_DELAY)


async def _resolved_by_team(session: AsyncSession, payment: Payment) -> bool:
    """Платёж уже разбирала команда — после применения клиент получает «Мы разобрались»."""
    by_team = await session.scalar(
        select(
            exists().where(
                JournalEntry.subject_type == PAYMENT_SUBJECT,
                JournalEntry.subject_id == str(payment.id),
                JournalEntry.actor_type == ActorType.TEAM_MEMBER,
            )
        )
    )
    return bool(by_team)


async def _notify_resolved(session: AsyncSession, payment: Payment, applied: Applied) -> None:
    """4.24: после решения команды клиент получает уведомление о результате."""
    if payment.client_id is None:
        return
    if applied.created:
        await send_to_client(session, payment.client_id, "event.payment_resolved.created")
        return
    subscription = await session.get(Subscription, applied.subscription_id)
    if subscription is None or subscription.expires_at is None:
        return
    texts = await load_texts(session)
    end_date = texts.date_fallback(
        subscription.expires_at,
        await get_setting(session, SHOP_LANGUAGE),
        await shop_time_zone(session),
    )
    await send_to_client(
        session,
        payment.client_id,
        "event.payment_resolved.renewed",
        variables={"subscription_name": subscription.name, "end_date": end_date},
    )


@task("payments.apply", PaymentArgs, cancellable=False)
async def apply_payment(context: TaskContext, args: PaymentArgs) -> None:
    """Применяет оплаченный платёж. Уже применённый или решённый — ничего не делает
    второй раз (сквозное правило 3)."""
    session = context.session
    payment = await session.get(Payment, args.payment_id, with_for_update=True)
    if payment is None or payment.state != PaymentState.PAID:
        return
    applied = await runtime.current().payments.apply(session, payment)
    payment.state = PaymentState.APPLIED
    payment.applied_at = datetime.now(UTC)
    payment.subscription_id = applied.subscription_id
    await journal_payment(
        session,
        payment,
        Actor.SYSTEM,
        "payment.applied",
        details={"subscription_id": applied.subscription_id, "created": applied.created},
    )
    if await _resolved_by_team(session, payment):
        await _notify_resolved(session, payment, applied)


@apply_payment.on_failed
async def _apply_failed(session: AsyncSession, _task_id: int, args: PaymentArgs) -> None:
    """Попытки исчерпаны — «оплачен — не применён», уведомление команде (4.20)."""
    payment = await session.get(Payment, args.payment_id, with_for_update=True)
    if payment is None or payment.state != PaymentState.PAID:
        return
    payment.state = PaymentState.PAID_NOT_APPLIED
    await journal_payment(
        session, payment, Actor.SYSTEM, "payment.not_applied", outcome=Outcome.FAILURE
    )
    await notify_team(
        session, "team.payment_not_applied", variables={"amount": await money(session, payment)}
    )


@task("payments.processing_notice", PaymentArgs, needs_attention=False)
async def processing_notice(context: TaskContext, args: PaymentArgs) -> None:
    """4.19: платёж оплачен, но не применён сразу — «Оплата получена, подписка создаётся»."""
    payment = await context.session.get(Payment, args.payment_id)
    if payment is None or payment.client_id is None:
        return
    if payment.state in (PaymentState.PAID, PaymentState.PAID_NOT_APPLIED):
        await send_to_client(context.session, payment.client_id, "event.payment_processing")
