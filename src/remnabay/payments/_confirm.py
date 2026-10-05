"""Провайдер подтвердил оплату (0008, 4.11, 4.23).

Вход общий для всех провайдеров: контракт провайдера (блок 3) разбирает вебхук или
опрос и вызывает `confirm_payment`. Деньги пришли — услуга будет: оплата по
истёкшему, отменённому или отклонённому ранее счёту применяется так же (3.9, 3.38).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment, PaymentState
from remnabay.journal import Actor, Outcome
from remnabay.messaging import notify_team
from remnabay.payments._apply import enqueue_apply, journal_payment, money

# Счёт ещё не оплачен с точки зрения магазина: подтверждение оплаты применяется
AWAITING_PAYMENT = (
    PaymentState.PENDING,
    PaymentState.EXPIRED,
    PaymentState.CANCELLED,
    PaymentState.DECLINED,
)


async def confirm_payment(
    session: AsyncSession,
    *,
    provider: str,
    provider_payment_id: str,
    amount: Decimal,
    currency: str,
) -> Payment:
    """Провайдер подтвердил оплату счёта. Повторное подтверждение ничего не делает
    второй раз (сквозное правило 3); оплата, которой магазин не знает, — в «Требуют
    внимания» с пометкой «неизвестный платёж» и уведомлением команде (4.23)."""
    actor = Actor.provider(provider)
    payment = await session.scalar(
        select(Payment)
        .where(Payment.provider == provider, Payment.provider_payment_id == provider_payment_id)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if payment is None:
        payment = Payment(
            is_unknown=True,
            state=PaymentState.PAID_NOT_APPLIED,
            amount=amount,
            currency=currency,
            provider=provider,
            provider_payment_id=provider_payment_id,
            paid_at=now,
        )
        session.add(payment)
        await session.flush()
        await journal_payment(session, payment, actor, "payment.unknown_received")
        await notify_team(
            session, "team.unknown_payment", variables={"amount": await money(session, payment)}
        )
        return payment
    if payment.state not in AWAITING_PAYMENT:
        await journal_payment(
            session,
            payment,
            actor,
            "payment.confirmation_repeated",
            outcome=Outcome.FAILURE,
        )
        return payment
    previous = payment.state
    payment.state = PaymentState.PAID
    payment.paid_at = now
    await journal_payment(
        session,
        payment,
        actor,
        "payment.paid",
        details={"previous": previous.value, "amount": str(amount), "currency": currency},
    )
    await enqueue_apply(session, payment)
    return payment
