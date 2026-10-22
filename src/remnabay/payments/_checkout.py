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
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from remnabay import runtime
from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff
from remnabay.journal import Actor, Outcome
from remnabay.messaging import MAIN_MENU_BUTTON, ButtonArgs, send_to_client
from remnabay.payments._apply import enqueue_apply, journal_payment
from remnabay.payments._catalog import check_choice, discounted_total
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
class CheckoutPromo:
    """Промокод на скидку с экрана подтверждения (8.4). Действует ли он, проверяет
    вызывающий; дальше скидка фиксируется в платеже (сквозное правило 2, 8.7)."""

    promo_code_id: int
    percent: int


@dataclass(frozen=True)
class Checkout:
    """Что клиент оплачивает: на экране подтверждения он видел итог `shown_total`."""

    client: Client
    purpose: PaymentPurpose
    tariff: Tariff
    # Продлеваемая подписка; у покупки — нет
    subscription: Subscription | None
    shown_total: Decimal
    # Токен экрана подтверждения: повторное «Оплатить» на нём не создаёт второй счёт (3.5)
    token: str
    # Назначение платежа на странице провайдера (`payment.description`)
    description: str
    # Куда вернуть клиента после оплаты — в бот (3.32)
    return_url: str
    # К одному платежу — не больше одного промокода (8.5)
    promo: CheckoutPromo | None = None


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


async def _operation_of(
    session: AsyncSession, checkout: Checkout, *, now: datetime, lifetime: timedelta
) -> UUID:
    """Номер операции нового счёта. Счёт той же операции, который ещё может быть
    оплачен или ждёт применения (в том числе истёкший, отменённый или оплаченный, но
    не применённый), передаёт ему свой номер: если оплатят оба, вторая оплата продлит
    подписку, созданную первой, а не создаст вторую (3.11). Операция, которую уже
    завершила применённая оплата, номер не передаёт: новый счёт — новая операция."""
    window = now - lifetime - await get_setting(session, POLL_AFTER_EXPIRY)
    applied = aliased(Payment)
    completed = exists().where(
        applied.operation_id == Payment.operation_id, applied.applied_at.is_not(None)
    )
    query = select(Payment.operation_id).where(
        Payment.client_id == checkout.client.id,
        Payment.purpose == checkout.purpose,
        Payment.applied_at.is_(None),
        Payment.operation_id.is_not(None),
        Payment.state.in_((*_OPEN_INVOICE, PaymentState.PAID, PaymentState.PAID_NOT_APPLIED)),
        Payment.created_at >= window,
        ~completed,
    )
    if checkout.subscription is None:
        query = query.where(Payment.subscription_id.is_(None))
    else:
        query = query.where(Payment.subscription_id == checkout.subscription.id)
    found = await session.scalar(query.order_by(Payment.id.desc()).limit(1))
    return found or uuid4()


async def open_invoice(
    session: AsyncSession, providers: Providers, checkout: Checkout, *, now: datetime
) -> Payment:
    """Создаёт платёж и счёт у провайдера на итог с экрана подтверждения (3.4, 8.4).

    Повторное нажатие на том же экране возвращает уже созданный платёж (3.5).
    Итог ноль (скидка 100%) — платёж без счёта у провайдера, подписка создаётся или
    продлевается сразу (8.8).
    `PriceChangedError` — итог изменился: экран подтверждения нужно показать заново.
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
    currency = await get_setting(session, SHOP_CURRENCY)
    available = await providers.available(session)
    # Один провайдер — без выбора (сквозное правило 5); выбор способа — с появлением второго
    provider = available[0] if available else None
    min_amount = provider.min_amount(currency) if provider is not None else Decimal(0)
    percent = checkout.promo.percent if checkout.promo is not None else None
    total = discounted_total(checkout.tariff.price, percent, min_amount)
    if total != checkout.shown_total:
        raise PriceChangedError("Итог к оплате изменился")
    if total > 0 and provider is None:
        raise PaymentUnavailableError("Нет доступного способа оплаты")

    replaced = await _unpaid_same_operation(session, checkout)
    lifetime = await get_setting(session, INVOICE_LIFETIME)
    operation_id = await _operation_of(session, checkout, now=now, lifetime=lifetime)
    snapshot = TariffSnapshot.of(checkout.tariff)
    payment = Payment(
        client_id=checkout.client.id,
        subscription_id=checkout.subscription.id if checkout.subscription else None,
        purpose=checkout.purpose,
        state=PaymentState.PENDING,
        tariff_id=checkout.tariff.id,
        tariff_snapshot=snapshot.stored(),
        promo_code_id=checkout.promo.promo_code_id if checkout.promo is not None else None,
        amount=total,
        discount_amount=snapshot.price - total,
        currency=currency,
        idempotency_key=checkout.token,
        operation_id=operation_id,
    )
    try:
        async with session.begin_nested():
            session.add(payment)
            if total == 0 or provider is None:
                # Итог ноль: оплачивать нечего, счёта у провайдера нет (8.8)
                payment.state = PaymentState.PAID
                payment.paid_at = now
                payment.paid_amount = total
                payment.paid_currency = currency
                await session.flush()
            else:
                payment.expires_at = now + lifetime
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
            "provider": payment.provider,
            "amount": str(payment.amount),
            "discount": str(payment.discount_amount),
            "promo_code_id": payment.promo_code_id,
            "currency": payment.currency,
            "tariff_id": checkout.tariff.id,
            "purpose": checkout.purpose.value,
        },
    )
    for old in replaced:
        old_provider = await providers.get(session, old.provider) if old.provider else None
        await _cancel(session, old_provider, old, actor, "payment.replaced", {"by": payment.id})
    if payment.provider is None:
        await enqueue_apply(session, payment)
        return payment
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


@dataclass(frozen=True)
class PayableInvoices:
    """Счета провайдера, которые клиенты ещё могут оплатить: магазин опрашивает их до
    истечения и ещё 24 часа после (3.29). `window_ends_at` — когда окно закончится
    у последнего из них."""

    count: int
    window_ends_at: datetime | None


async def payable_invoices(
    session: AsyncSession, provider: str, *, now: datetime
) -> PayableInvoices:
    """Если отключить провайдера раньше конца окна, поздняя оплата не применится сама,
    пока ключи не введут снова (3.42)."""
    after = await get_setting(session, POLL_AFTER_EXPIRY)
    count, latest = (
        await session.execute(
            select(func.count(), func.max(Payment.expires_at)).where(
                Payment.provider == provider,
                Payment.provider_payment_id.is_not(None),
                # Отказ ставится только по сообщению провайдера — такой счёт у него
                # закрыт. Истёкший и отменённый у провайдера могут быть ещё открыты
                Payment.state.in_(set(_OPEN_INVOICE) - {PaymentState.DECLINED}),
                # Окно опроса — то же, что у poll_invoice
                Payment.expires_at >= now - after,
            )
        )
    ).one()
    return PayableInvoices(count=count, window_ends_at=latest + after if latest else None)


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
