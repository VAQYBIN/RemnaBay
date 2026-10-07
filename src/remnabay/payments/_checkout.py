"""Счёт у провайдера: создание, замена, отмена, истечение и опрос статуса (блок 3).

Условия фиксируются при создании платежа (сквозное правило 2). Один платёж — один
счёт (3.34). Двойное «Оплатить» на одном экране подтверждения не создаёт второй
счёт (3.5); новый счёт за ту же операцию отменяет неоплаченный прежний (3.10).
Если провайдер недоступен, ничего не создаётся (3.30). Деньги пришли по
истёкшему или отменённому счёту — платёж применяется (0008, 3.9, 3.38).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff
from remnabay.journal import Actor, Outcome
from remnabay.messaging import MAIN_MENU_BUTTON, ButtonArgs, send_to_client
from remnabay.payments._apply import journal_payment
from remnabay.payments._catalog import check_choice
from remnabay.payments._confirm import confirm_payment
from remnabay.payments._provider import (
    InvoiceRequest,
    PaymentProvider,
    ProviderError,
    ProviderPayment,
    ProviderStatus,
)
from remnabay.payments._providers import Providers
from remnabay.payments._snapshot import TariffSnapshot
from remnabay.queue import TaskContext, task
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import ShopSetting, get_setting

_DURATION = TypeAdapter[timedelta](Annotated[timedelta, Field(gt=timedelta(0))])

# «Оплата» → «Время жизни счёта» (3.8)
INVOICE_LIFETIME = ShopSetting("payments.invoice_lifetime", _DURATION, timedelta(minutes=30))
# «Оплата» → «Опрос неоплаченных счетов»: каждые 5 минут, до истечения счёта и
# ещё 24 часа после (3.29)
POLL_INTERVAL = ShopSetting("payments.poll_interval", _DURATION, timedelta(minutes=5))
POLL_AFTER_EXPIRY = ShopSetting("payments.poll_after_expiry", _DURATION, timedelta(hours=24))

# Состояния, в которых счёт ещё может быть оплачен с точки зрения магазина
_OPEN_INVOICE = (
    PaymentState.PENDING,
    PaymentState.EXPIRED,
    PaymentState.CANCELLED,
    PaymentState.DECLINED,
)


class CheckoutError(Exception):
    """Счёт не создан."""


class PriceChangedError(CheckoutError):
    """Цена изменилась с тех пор, как клиент видел экран подтверждения (3.4)."""


class PaymentUnavailableError(CheckoutError):
    """Нет доступного провайдера или он не отвечает (3.30)."""


@dataclass(frozen=True)
class Checkout:
    """Что клиент оплачивает: на экране подтверждения он видел `shown_price`."""

    client: Client
    purpose: PaymentPurpose
    tariff: Tariff
    # Продлеваемая подписка; у покупки — нет
    subscription: Subscription | None
    shown_price: Decimal
    # Токен экрана подтверждения: повторное «Оплатить» на нём не создаёт второй счёт (3.5)
    token: str
    # Назначение платежа на странице провайдера (`payment.description`)
    description: str
    # Куда вернуть клиента после оплаты — в бот (3.32)
    return_url: str


async def _same_token(session: AsyncSession, token: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.idempotency_key == token))


async def _unpaid_same_operation(session: AsyncSession, checkout: Checkout) -> list[Payment]:
    """Неоплаченные счета той же операции: та же покупка или продление той же
    подписки (3.10). Счета за другие операции не затрагиваются."""
    query = select(Payment).where(
        Payment.state == PaymentState.PENDING,
        Payment.purpose == checkout.purpose,
        Payment.client_id == checkout.client.id,
    )
    if checkout.subscription is None:
        query = query.where(Payment.subscription_id.is_(None))
    else:
        query = query.where(Payment.subscription_id == checkout.subscription.id)
    result = await session.scalars(query.order_by(Payment.id).with_for_update())
    return list(result)


async def open_invoice(
    session: AsyncSession, providers: Providers, checkout: Checkout, *, now: datetime
) -> Payment:
    """Создаёт платёж и счёт у провайдера на сумму с экрана подтверждения (3.4).

    Повторное нажатие на том же экране возвращает уже созданный платёж (3.5).
    `PriceChangedError` — цена изменилась: экран подтверждения нужно показать заново.
    `PaymentUnavailableError` — провайдер недоступен, ничего не создано (3.30).
    """
    # Счета одного клиента создаются по очереди: иначе два экрана подтверждения
    # не увидели бы счета друг друга (3.10)
    await session.get(Client, checkout.client.id, with_for_update=True)
    existing = await _same_token(session, checkout.token)
    if existing is not None:
        if existing.client_id != checkout.client.id:
            raise CheckoutError("Чужой экран подтверждения")
        return existing
    await check_choice(
        session,
        client_id=checkout.client.id,
        purpose=checkout.purpose,
        tariff=checkout.tariff,
        subscription=checkout.subscription,
        now=now,
    )
    if checkout.tariff.price != checkout.shown_price:
        raise PriceChangedError("Цена тарифа изменилась")
    available = await providers.available(session)
    if not available:
        raise PaymentUnavailableError("Нет доступного способа оплаты")
    # Один провайдер — без выбора (сквозное правило 5); выбор способа — с появлением второго
    provider = available[0]

    replaced = await _unpaid_same_operation(session, checkout)
    lifetime = await get_setting(session, INVOICE_LIFETIME)
    snapshot = TariffSnapshot.of(checkout.tariff)
    payment = Payment(
        client_id=checkout.client.id,
        subscription_id=checkout.subscription.id if checkout.subscription else None,
        purpose=checkout.purpose,
        state=PaymentState.PENDING,
        tariff_id=checkout.tariff.id,
        tariff_snapshot=snapshot.stored(),
        amount=snapshot.price,
        currency=await get_setting(session, SHOP_CURRENCY),
        idempotency_key=checkout.token,
        operation_id=replaced[0].operation_id if replaced else uuid4(),
        expires_at=now + lifetime,
    )
    try:
        async with session.begin_nested():
            session.add(payment)
            await session.flush()
            invoice = await provider.create_invoice(
                InvoiceRequest(
                    payment_id=payment.id,
                    amount=payment.amount,
                    currency=payment.currency,
                    description=checkout.description,
                    idempotency_key=checkout.token,
                    return_url=checkout.return_url,
                )
            )
            payment.provider = provider.code
            payment.provider_payment_id = invoice.provider_payment_id
            payment.payment_url = invoice.payment_url
            await session.flush()
    except IntegrityError:
        # Второе одновременное нажатие на том же экране: первое уже создало платёж
        existing = await _same_token(session, checkout.token)
        if existing is None:
            raise
        return existing
    except ProviderError as error:
        raise PaymentUnavailableError(str(error)) from error

    actor = Actor.client(checkout.client.id)
    await journal_payment(
        session,
        payment,
        actor,
        "payment.created",
        details={
            "provider": provider.code,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "tariff_id": checkout.tariff.id,
            "purpose": checkout.purpose.value,
        },
    )
    for old in replaced:
        await _cancel(session, provider, old, actor, "payment.replaced", {"by": payment.id})
    await expire_invoice.enqueue(session, InvoiceArgs(payment_id=payment.id), delay=lifetime)
    await poll_invoice.enqueue(
        session, InvoiceArgs(payment_id=payment.id), delay=await get_setting(session, POLL_INTERVAL)
    )
    return payment


async def _cancel(
    session: AsyncSession,
    provider: PaymentProvider | None,
    payment: Payment,
    actor: Actor,
    action: str,
    details: dict[str, str | int],
) -> None:
    """Неоплаченный счёт — «отменён» в магазине и у провайдера, если провайдер это
    умеет (3.10, 3.38). Отказ провайдера не мешает: если деньги всё же придут, оплата
    применится (0008)."""
    payment.state = PaymentState.CANCELLED
    cancelled_by_provider: bool | str = False
    if provider is not None and payment.provider_payment_id is not None:
        try:
            cancelled_by_provider = await provider.cancel_invoice(payment.provider_payment_id)
        except ProviderError as error:
            cancelled_by_provider = f"ошибка: {error}"
    await journal_payment(
        session,
        payment,
        actor,
        action,
        details={**details, "cancelled_by_provider": cancelled_by_provider},
    )


async def cancel_by_client(
    session: AsyncSession, providers: Providers, payment_id: int, *, client_id: int
) -> Payment | None:
    """«Отменить» на экране оплаты (3.38). Уже оплаченный или закрытый счёт не меняется."""
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None or payment.client_id != client_id:
        return None
    if payment.state != PaymentState.PENDING:
        return payment
    provider = await providers.get(session, payment.provider) if payment.provider else None
    await _cancel(session, provider, payment, Actor.client(client_id), "payment.cancelled", {})
    return payment


class InvoiceArgs(BaseModel):
    payment_id: int


@task("payments.expire", InvoiceArgs, needs_attention=False)
async def expire_invoice(context: TaskContext, args: InvoiceArgs) -> None:
    """Счёт не оплачен за время жизни — «истёк» (3.8). Если деньги придут позже,
    оплата применится (3.9): опрос продолжается ещё 24 часа."""
    session = context.session
    payment = await session.get(Payment, args.payment_id, with_for_update=True)
    if payment is None or payment.state != PaymentState.PENDING:
        return
    payment.state = PaymentState.EXPIRED
    await journal_payment(session, payment, Actor.SYSTEM, "payment.expired")


async def decline(session: AsyncSession, payment: Payment, actor: Actor) -> None:
    """Провайдер отказал в оплате (3.12): клиент видит «Платёж не прошёл» и может
    попробовать снова. Сообщение — только по ещё ждущему оплаты счёту."""
    if payment.state != PaymentState.PENDING:
        return
    payment.state = PaymentState.DECLINED
    await journal_payment(session, payment, actor, "payment.declined")
    if payment.client_id is not None:
        await send_to_client(
            session,
            payment.client_id,
            "event.payment_failed",
            buttons=[
                ButtonArgs(text_key="btn.try_again", callback_data=retry_callback(payment.id)),
                MAIN_MENU_BUTTON,
            ],
        )


def retry_callback(payment_id: int) -> str:
    """Данные кнопки «Попробовать снова»: бот снова показывает подтверждение той же
    операции (03-screens, С5)."""
    return f"payretry:{payment_id}"


async def provider_reported(
    session: AsyncSession, provider_code: str, reported: ProviderPayment
) -> Payment | None:
    """Проверенное состояние счёта от провайдера — из вебхука или опроса (3.28, 3.29)."""
    actor = Actor.provider(provider_code)
    if reported.status == ProviderStatus.SUCCEEDED:
        return await confirm_payment(
            session,
            provider=provider_code,
            provider_payment_id=reported.provider_payment_id,
            amount=reported.amount,
            currency=reported.currency,
        )
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.provider == provider_code,
            Payment.provider_payment_id == reported.provider_payment_id,
        )
        .with_for_update()
    )
    if payment is None:
        return None
    if reported.status == ProviderStatus.DECLINED:
        await decline(session, payment, actor)
    elif reported.status == ProviderStatus.EXPIRED and payment.state == PaymentState.PENDING:
        payment.state = PaymentState.EXPIRED
        await journal_payment(session, payment, actor, "payment.expired")
    return payment


@task("payments.poll", InvoiceArgs, needs_attention=False)
async def poll_invoice(context: TaskContext, args: InvoiceArgs) -> None:
    """Опрос статуса счёта, если подтверждение от провайдера не дошло (3.29): каждые
    5 минут до истечения счёта и ещё 24 часа после."""
    session = context.session
    payment = await session.get(Payment, args.payment_id)
    if (
        payment is None
        or payment.state not in _OPEN_INVOICE
        or payment.provider is None
        or payment.provider_payment_id is None
        or payment.expires_at is None
    ):
        return
    now = datetime.now(UTC)
    if now > payment.expires_at + await get_setting(session, POLL_AFTER_EXPIRY):
        await journal_payment(
            session, payment, Actor.SYSTEM, "payment.polling_stopped", outcome=Outcome.FAILURE
        )
        return
    provider = await runtime.current().providers.get(session, payment.provider)
    reported: ProviderPayment | None = None
    if provider is not None:
        try:
            reported = await provider.fetch_payment(payment.provider_payment_id)
        except ProviderError:
            # Провайдер не ответил — спросим в следующий раз
            reported = None
    if reported is not None:
        await provider_reported(session, payment.provider, reported)
        if reported.status != ProviderStatus.PENDING:
            # Счёт у провайдера закрыт — оплаченным или нет: спрашивать больше нечего
            return
    await poll_invoice.enqueue(session, args, delay=await get_setting(session, POLL_INTERVAL))
