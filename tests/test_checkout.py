"""Счёт у провайдера: создание, замена, отмена, истечение, опрос, вебхук, сверка суммы.

Блок 3: 3.4, 3.5, 3.7–3.10, 3.12, 3.27–3.30, 3.33, 3.34, 3.38, 3.39.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox, generate_key
from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Subject, entries_for
from remnabay.messaging import SendArgs
from remnabay.payments import (
    CatalogError,
    Checkout,
    ConnectionState,
    PaymentUnavailableError,
    PriceChangedError,
    Providers,
    ProviderStatus,
    TariffSnapshot,
    cancel_by_client,
    confirm_payment,
    expire_invoice,
    open_invoice,
    payment_subject,
    poll_invoice,
    provider_reported,
)
from remnabay.queue import TaskContext
from remnabay.queue._core import RunnableTask
from remnabay.queue._models import QueueTask
from remnabay.shop import SHOP_CURRENCY
from tests.domain_support import add, make_client, make_subscription, make_tariff, put_setting
from tests.panel_support import fake_runtime
from tests.payment_support import (
    FAKE,
    SIGNATURE,
    VALID_SIGNATURE,
    FakeKind,
    FakeProvider,
    connect_fake,
    fake_providers,
)
from tests.web_support import Shop, running_shop

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def kind() -> FakeKind:
    return FakeKind()


@pytest.fixture
def provider(kind: FakeKind) -> FakeProvider:
    return kind.provider


@pytest.fixture
def providers(kind: FakeKind) -> Providers:
    return fake_providers(kind)


@pytest.fixture
async def client(db_session: AsyncSession) -> Client:
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
    await connect_fake(db_session)
    return await make_client(db_session, telegram_id=777)


@pytest.fixture
async def tariff(db_session: AsyncSession) -> Tariff:
    tariff = make_tariff()
    await add(db_session, tariff)
    return tariff


def _purchase(
    client: Client, tariff: Tariff, screen: str = "screen-1", price: Decimal | None = None
) -> Checkout:
    return Checkout(
        client=client,
        purpose=PaymentPurpose.PURCHASE,
        tariff=tariff,
        subscription=None,
        shown_price=tariff.price if price is None else price,
        token=screen,
        description="Магазин: Месяц, 30 дней",
        return_url="https://t.me/shop_bot",
    )


def _renewal(client: Client, tariff: Tariff, subscription: Subscription, screen: str) -> Checkout:
    return Checkout(
        client=client,
        purpose=PaymentPurpose.RENEWAL,
        tariff=tariff,
        subscription=subscription,
        shown_price=tariff.price,
        token=screen,
        description="Магазин: Месяц, 30 дней",
        return_url="https://t.me/shop_bot",
    )


async def _payments(session: AsyncSession) -> list[Payment]:
    return list(await session.scalars(select(Payment).order_by(Payment.id)))


async def _tasks(session: AsyncSession) -> list[str]:
    return list(await session.scalars(select(QueueTask.name).order_by(QueueTask.id)))


async def _messages(session: AsyncSession) -> list[SendArgs]:
    rows = await session.scalars(
        select(QueueTask.args).where(QueueTask.name == "messages.send").order_by(QueueTask.id)
    )
    return [SendArgs.model_validate(args) for args in rows]


async def _actions(session: AsyncSession, payment: Payment) -> list[str]:
    return [entry.action for entry in await entries_for(session, payment_subject(payment.id))]


async def _run(
    session: AsyncSession, providers: Providers, handler: RunnableTask, payment: Payment
) -> None:
    context = TaskContext(session=session, task_id=1, attempt_number=1)
    with fake_runtime(providers=providers):
        await handler.run(context, {"payment_id": payment.id})
    await session.flush()


# --- Создание счёта ---


async def test_3_4_pay_creates_invoice_for_amount_on_confirmation_screen(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.4, 3.34: «Оплатить» — счёт у провайдера на сумму с экрана подтверждения;
    платёж «ожидает оплаты», переход в журнале."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)

    assert payment.state == PaymentState.PENDING
    assert payment.amount == Decimal("199.00")
    assert payment.provider == FAKE
    assert payment.payment_url == f"https://pay.example/{payment.provider_payment_id}"
    assert payment.expires_at == NOW + timedelta(minutes=30)
    [request] = provider.requests
    assert (request.amount, request.currency, request.payment_id) == (
        Decimal("199.00"),
        "RUB",
        payment.id,
    )
    assert request.return_url == "https://t.me/shop_bot"
    assert await _actions(db_session, payment) == ["payment.created"]
    assert await _tasks(db_session) == ["payments.expire", "payments.poll"]


async def test_3_4_changed_price_shows_confirmation_again_without_invoice(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.4: счёт — только на сумму, которую клиент видел; цена изменилась — счёта нет."""
    with pytest.raises(PriceChangedError):
        await open_invoice(
            db_session, providers, _purchase(client, tariff, price=Decimal("99.00")), now=NOW
        )
    assert await _payments(db_session) == []
    assert provider.requests == []


async def test_3_5_double_pay_on_one_screen_creates_one_invoice(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.5: двойное «Оплатить» на одном экране подтверждения — один счёт."""
    first = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    second = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)

    assert second.id == first.id
    assert len(await _payments(db_session)) == 1
    assert len(provider.requests) == 1


async def test_3_7_conditions_fixed_when_invoice_created(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.7, 2.6: условия тарифа фиксируются при создании счёта — правка тарифа их не меняет."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    tariff.price = Decimal("299.00")
    tariff.duration_days = 60
    tariff.device_limit = 5
    await db_session.flush()

    snapshot = TariffSnapshot.of_payment(payment)
    assert (snapshot.price, snapshot.duration_days, snapshot.device_limit) == (
        Decimal("199.00"),
        30,
        3,
    )
    assert payment.amount == Decimal("199.00")


async def test_3_30_provider_unavailable_creates_nothing(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.30: провайдер недоступен — «Оплата временно недоступна», ничего не создаётся."""
    provider.unavailable = True
    with pytest.raises(PaymentUnavailableError):
        await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    assert await _payments(db_session) == []
    assert await _tasks(db_session) == []


async def test_1_14_no_connected_provider_means_payment_unavailable(
    db_session: AsyncSession, tariff: Tariff
) -> None:
    """1.14, 3.30: без подключённого способа оплаты счёт не создаётся."""
    client = await make_client(db_session, telegram_id=778)
    with pytest.raises(PaymentUnavailableError):
        await open_invoice(db_session, fake_providers(), _purchase(client, tariff), now=NOW)


async def test_3_33_provider_without_shop_currency_is_unavailable(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.33: провайдер, который не принимает валюту учёта, недоступен для выбора."""
    await put_setting(db_session, SHOP_CURRENCY, "USD")
    [connection] = await providers.connections(db_session)

    assert connection.state == ConnectionState.CURRENCY_UNSUPPORTED
    assert await providers.available(db_session) == []
    with pytest.raises(PaymentUnavailableError):
        await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)


async def test_0049_keys_under_other_encryption_key_need_to_be_entered_again(
    db_session: AsyncSession, kind: FakeKind
) -> None:
    """0049: ключи провайдера не расшифровываются — способ оплаты недоступен, в админке
    «Ключи нужно ввести заново», в журнале — ошибка расшифровки."""
    await connect_fake(db_session, SecretBox(generate_key()))
    providers = fake_providers(kind)
    [connection] = await providers.connections(db_session)

    assert connection.state == ConnectionState.KEYS_LOST
    assert await providers.available(db_session) == []


async def test_3_2_archived_tariff_cannot_be_bought(
    db_session: AsyncSession, providers: Providers, client: Client
) -> None:
    """3.2: тариф в архиве не продаётся новым клиентам — даже по старой кнопке."""
    archived = make_tariff(state=TariffState.ARCHIVED)
    await add(db_session, archived)
    with pytest.raises(CatalogError):
        await open_invoice(db_session, providers, _purchase(client, archived), now=NOW)


# --- Замена и отмена счёта ---


async def test_3_10_new_invoice_cancels_unpaid_one_for_same_operation(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.10: новый счёт за ту же покупку отменяет неоплаченный прежний — в магазине и у
    провайдера; счета одной операции разделяют её номер (для 3.11)."""
    old = await open_invoice(db_session, providers, _purchase(client, tariff, "s-1"), now=NOW)
    new = await open_invoice(db_session, providers, _purchase(client, tariff, "s-2"), now=NOW)

    assert old.state == PaymentState.CANCELLED
    assert new.state == PaymentState.PENDING
    assert new.operation_id == old.operation_id
    assert provider.cancelled == [old.provider_payment_id]
    assert await _actions(db_session, old) == ["payment.created", "payment.replaced"]


async def test_3_10_invoices_for_other_operations_are_not_touched(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.10: счета за другие операции (продление другой подписки) не затрагиваются."""
    first = make_subscription(client, tariff, panel_user_id=1)
    second = make_subscription(client, tariff, panel_user_id=2)
    for subscription in (first, second):
        subscription.expires_at = NOW + timedelta(days=5)
    await add(db_session, first, second)

    one = await open_invoice(db_session, providers, _renewal(client, tariff, first, "r-1"), now=NOW)
    two = await open_invoice(
        db_session, providers, _renewal(client, tariff, second, "r-2"), now=NOW
    )

    assert (one.state, two.state) == (PaymentState.PENDING, PaymentState.PENDING)
    assert one.operation_id != two.operation_id


async def test_3_10_replacement_keeps_working_when_provider_cannot_cancel(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.10: провайдер не умеет отменять счёт — счёт отменяется только в магазине."""
    provider.can_cancel = False
    old = await open_invoice(db_session, providers, _purchase(client, tariff, "s-1"), now=NOW)
    await open_invoice(db_session, providers, _purchase(client, tariff, "s-2"), now=NOW)

    assert old.state == PaymentState.CANCELLED
    assert provider.cancelled == []


async def test_3_38_client_cancels_invoice_and_late_money_still_applies(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.38: «Отменить» на экране оплаты — платёж «отменён», счёт отменён у провайдера;
    если деньги всё же пришли, оплата применяется (0008)."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    await cancel_by_client(db_session, providers, payment.id, client_id=client.id)

    assert payment.state == PaymentState.CANCELLED
    assert provider.cancelled == [payment.provider_payment_id]

    assert payment.provider_payment_id is not None
    reported = provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)
    await provider_reported(db_session, FAKE, reported)
    assert payment.state == PaymentState.PAID
    assert "payments.apply" in await _tasks(db_session)


async def test_3_38_client_cannot_cancel_paid_invoice(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.38: оплаченный счёт кнопкой «Отменить» не отменяется."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    payment.state = PaymentState.PAID
    await cancel_by_client(db_session, providers, payment.id, client_id=client.id)
    assert payment.state == PaymentState.PAID


# --- Истечение, отказ, опрос ---


async def test_3_8_unpaid_invoice_expires_without_subscription(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.8: счёт не оплачен за время жизни — «истёк», подписка не создаётся."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    await _run(db_session, providers, expire_invoice, payment)

    assert payment.state == PaymentState.EXPIRED
    assert "payments.apply" not in await _tasks(db_session)
    assert await db_session.scalar(select(Subscription)) is None


async def test_3_9_payment_for_expired_invoice_is_applied(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.9, 3.29: оплата по истёкшему счёту, найденная опросом, применяется как обычно."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    await _run(db_session, providers, expire_invoice, payment)
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)
    payment.expires_at = datetime.now(UTC) - timedelta(hours=1)

    await _run(db_session, providers, poll_invoice, payment)

    assert payment.state == PaymentState.PAID
    assert "payments.apply" in await _tasks(db_session)


async def test_3_12_declined_payment_tells_client_and_offers_retry(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.12: провайдер отклонил оплату — клиент видит «Платёж не прошёл» с «Попробовать
    снова»; подписка не создаётся."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    assert payment.provider_payment_id is not None
    reported = provider.set(payment.provider_payment_id, ProviderStatus.DECLINED)
    await provider_reported(db_session, FAKE, reported)

    assert payment.state == PaymentState.DECLINED
    [message] = await _messages(db_session)
    assert message.text_key == "event.payment_failed"
    assert [button.text_key for button in message.buttons] == ["btn.try_again", "btn.main_menu"]
    assert message.buttons[0].callback_data == f"payretry:{payment.id}"
    assert "payments.apply" not in await _tasks(db_session)


async def test_3_29_unpaid_invoice_is_polled_again(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.29: подтверждения нет, счёт ещё ждёт оплаты — магазин спросит снова."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    payment.expires_at = datetime.now(UTC) + timedelta(minutes=20)
    await _run(db_session, providers, poll_invoice, payment)

    assert payment.state == PaymentState.PENDING
    assert await _tasks(db_session) == ["payments.expire", "payments.poll", "payments.poll"]


async def test_3_29_polling_continues_when_provider_does_not_answer(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.29: провайдер не ответил на опрос — магазин спросит в следующий раз."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    payment.expires_at = datetime.now(UTC)
    provider.unavailable = True
    await _run(db_session, providers, poll_invoice, payment)

    assert (await _tasks(db_session)).count("payments.poll") == 2


async def test_3_29_polling_stops_24_hours_after_expiry(
    db_session: AsyncSession,
    providers: Providers,
    provider: FakeProvider,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.29: статус спрашивается до истечения счёта и ещё 24 часа после — не дольше."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    payment.expires_at = datetime.now(UTC) - timedelta(hours=25)
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)
    await _run(db_session, providers, poll_invoice, payment)

    assert payment.state == PaymentState.PENDING
    assert (await _tasks(db_session)).count("payments.poll") == 1
    assert "payment.polling_stopped" in await _actions(db_session, payment)


# --- Сверка суммы (3.39) ---


@pytest.mark.parametrize(
    ("amount", "currency"),
    [(Decimal("198.99"), "RUB"), (Decimal("199.01"), "RUB"), (Decimal("199.00"), "USD")],
)
async def test_3_39_amount_mismatch_is_not_applied_and_team_notified(
    db_session: AsyncSession,
    providers: Providers,
    client: Client,
    tariff: Tariff,
    amount: Decimal,
    currency: str,
) -> None:
    """3.39: сумма или валюта в подтверждении не совпала со счётом с точностью до копейки —
    платёж сразу «оплачен — не применён» с пометкой «сумма не совпала», команда получает
    уведомление, автоматического применения нет."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    assert payment.provider_payment_id is not None
    await confirm_payment(
        db_session,
        provider=FAKE,
        provider_payment_id=payment.provider_payment_id,
        amount=amount,
        currency=currency,
    )

    assert payment.state == PaymentState.PAID_NOT_APPLIED
    assert payment.amount_mismatch
    assert (payment.paid_amount, payment.paid_currency) == (amount, currency)
    assert "payments.apply" not in await _tasks(db_session)
    assert "payment.amount_mismatch" in await _actions(db_session, payment)
    [notice] = await _messages(db_session)
    assert notice.text_key == "team.payment_amount_mismatch"
    assert notice.variables["amount"] == "199,00\xa0₽"


async def test_3_39_matching_amount_is_applied(
    db_session: AsyncSession, providers: Providers, client: Client, tariff: Tariff
) -> None:
    """3.39: сумма совпала до копейки — обычное применение."""
    payment = await open_invoice(db_session, providers, _purchase(client, tariff), now=NOW)
    assert payment.provider_payment_id is not None
    await confirm_payment(
        db_session,
        provider=FAKE,
        provider_payment_id=payment.provider_payment_id,
        amount=Decimal("199.00"),
        currency="RUB",
    )
    assert payment.state == PaymentState.PAID
    assert not payment.amount_mismatch
    assert "payments.apply" in await _tasks(db_session)


# --- Вебхук провайдера (3.28) ---


@pytest.fixture
async def shop(
    valid_env: dict[str, str], db_session: AsyncSession, providers: Providers
) -> AsyncGenerator[Shop]:
    async with running_shop(db_session) as shop:
        shop.use_providers(providers)
        yield shop


async def test_3_28_verified_notification_confirms_payment(
    shop: Shop, providers: Providers, provider: FakeProvider, client: Client, tariff: Tariff
) -> None:
    """3.28: подтверждение, проверенное по правилам провайдера, — платёж оплачен."""
    payment = await open_invoice(shop.session, providers, _purchase(client, tariff), now=NOW)
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)

    response = await shop.http.post(
        f"/webhooks/payments/{FAKE}",
        content=json.dumps({"id": payment.provider_payment_id}),
        headers={SIGNATURE: VALID_SIGNATURE},
    )

    assert response.status_code == 200
    assert payment.state == PaymentState.PAID


async def test_3_28_unverified_notification_is_rejected_and_journaled(
    shop: Shop, providers: Providers, provider: FakeProvider, client: Client, tariff: Tariff
) -> None:
    """3.28: непроверенное подтверждение отклоняется и записывается в журнал."""
    payment = await open_invoice(shop.session, providers, _purchase(client, tariff), now=NOW)
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)

    response = await shop.http.post(
        f"/webhooks/payments/{FAKE}",
        content=json.dumps({"id": payment.provider_payment_id}),
        headers={SIGNATURE: "forged"},
    )

    assert response.status_code == 400
    assert payment.state == PaymentState.PENDING
    [entry] = await entries_for(shop.session, Subject("payment_provider", FAKE))
    assert entry.action == "payment.notification_rejected"


async def test_3_28_unknown_provider_webhook_is_not_found(shop: Shop) -> None:
    """Адрес вебхука есть только у провайдеров, которые знает магазин."""
    response = await shop.http.post("/webhooks/payments/nobody", content=b"{}")
    assert response.status_code == 404
