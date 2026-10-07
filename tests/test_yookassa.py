"""ЮКасса через контракт провайдера (3.31, 3.32; 3.4–3.12, 3.28–3.30 через подделку API)."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.payments import (
    Checkout,
    InvoiceRequest,
    NotificationError,
    PaymentUnavailableError,
    ProviderAuthError,
    Providers,
    ProviderStatus,
    ProviderUnavailableError,
    open_invoice,
)
from remnabay.payments.yookassa import (
    YOOKASSA,
    YooKassaCredentials,
    YooKassaKind,
    YooKassaProvider,
)
from tests.domain_support import add, make_client, make_tariff
from tests.payment_support import (
    YOOKASSA_SECRET,
    YOOKASSA_SHOP_ID,
    FakeYooKassa,
    connect_yookassa,
    shop_box,
)
from tests.web_support import Shop, running_shop

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RETURN_URL = "https://t.me/shop_bot"


@pytest.fixture
def yookassa() -> FakeYooKassa:
    return FakeYooKassa()


@pytest.fixture
async def http(yookassa: FakeYooKassa) -> AsyncGenerator[httpx2.AsyncClient]:
    async with yookassa.http() as client:
        yield client


@pytest.fixture
def provider(http: httpx2.AsyncClient) -> YooKassaProvider:
    credentials = YooKassaCredentials(shop_id=YOOKASSA_SHOP_ID, secret_key=YOOKASSA_SECRET)
    return YooKassaProvider(http, credentials)


@pytest.fixture
def providers(http: httpx2.AsyncClient) -> Providers:
    return Providers(shop_box(), [YooKassaKind(http)])


def _request(key: str = "screen-1", description: str = "Магазин: Месяц, 30 дней") -> InvoiceRequest:
    return InvoiceRequest(
        payment_id=42,
        amount=Decimal("199.00"),
        currency="RUB",
        description=description,
        idempotency_key=key,
        return_url=RETURN_URL,
    )


def _notification(payment_id: str, status: str = "succeeded") -> bytes:
    body = {
        "type": "notification",
        "event": f"payment.{status}",
        "object": {
            "id": payment_id,
            "status": status,
            "amount": {"value": "1.00", "currency": "RUB"},
        },
    }
    return json.dumps(body).encode()


async def test_3_31_invoice_is_created_in_yookassa(
    provider: YooKassaProvider, yookassa: FakeYooKassa
) -> None:
    """3.31, 3.4, 3.32: счёт в ЮКассе — на сумму в валюте учёта, со списанием сразу и
    возвратом клиента в бот после оплаты; ключи магазина — в заголовке."""
    invoice = await provider.create_invoice(_request())

    [request] = yookassa.requests
    body = json.loads(request.content)
    assert body["amount"] == {"value": "199.00", "currency": "RUB"}
    assert body["capture"] is True
    assert body["confirmation"] == {"type": "redirect", "return_url": RETURN_URL}
    assert body["metadata"] == {"payment_id": "42"}
    assert request.headers["Idempotence-Key"] == "screen-1"
    assert invoice.payment_url == f"https://yoomoney.example/pay/{invoice.provider_payment_id}"


async def test_3_5_same_key_gives_same_yookassa_payment(provider: YooKassaProvider) -> None:
    """3.5: повтор с тем же ключом идемпотентности не создаёт второй платёж."""
    first = await provider.create_invoice(_request())
    second = await provider.create_invoice(_request())
    assert first.provider_payment_id == second.provider_payment_id


async def test_0057_description_fits_yookassa_limit(
    provider: YooKassaProvider, yookassa: FakeYooKassa
) -> None:
    """Решение 0057: назначение платежа не длиннее 128 символов."""
    await provider.create_invoice(_request(description="Ж" * 300))
    assert len(json.loads(yookassa.requests[0].content)["description"]) == 128


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("pending", None, ProviderStatus.PENDING),
        ("succeeded", None, ProviderStatus.SUCCEEDED),
        ("canceled", "insufficient_funds", ProviderStatus.DECLINED),
        ("canceled", "general_decline", ProviderStatus.DECLINED),
        ("canceled", "expired_on_confirmation", ProviderStatus.EXPIRED),
    ],
)
async def test_3_29_yookassa_status_is_read(
    provider: YooKassaProvider,
    yookassa: FakeYooKassa,
    status: str,
    reason: str | None,
    expected: ProviderStatus,
) -> None:
    """3.29, 3.12, 3.8: статус счёта — оплачен, отклонён или истёк."""
    invoice = await provider.create_invoice(_request())
    if status == "succeeded":
        yookassa.succeed(invoice.provider_payment_id)
    elif status == "canceled" and reason is not None:
        yookassa.cancel(invoice.provider_payment_id, reason)

    reported = await provider.fetch_payment(invoice.provider_payment_id)
    assert reported.status == expected
    assert (reported.amount, reported.currency) == (Decimal("199.00"), "RUB")


async def test_3_39_yookassa_reports_amount_paid_by_client(
    provider: YooKassaProvider, yookassa: FakeYooKassa
) -> None:
    """3.39: магазин сверяет со счётом сумму, которую подтвердила ЮКасса."""
    invoice = await provider.create_invoice(_request())
    yookassa.succeed(invoice.provider_payment_id, amount="150.00")
    reported = await provider.fetch_payment(invoice.provider_payment_id)
    assert reported.amount == Decimal("150.00")


async def test_3_28_notification_is_checked_by_api_not_by_its_body(
    provider: YooKassaProvider,
) -> None:
    """3.28, решение 0057: уведомление не подписано — статус берётся из API ЮКассы.
    Поддельное «оплачено» по неоплаченному счёту не делает его оплаченным."""
    invoice = await provider.create_invoice(_request())
    reported = await provider.verify_notification(_notification(invoice.provider_payment_id), {})
    assert reported.status == ProviderStatus.PENDING


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"type": "other"}', _notification("yk-unknown")],
)
async def test_3_28_unverifiable_notification_is_rejected(
    provider: YooKassaProvider, body: bytes
) -> None:
    """3.28: уведомление, которое ЮКасса не подтверждает, отклоняется."""
    with pytest.raises(NotificationError):
        await provider.verify_notification(body, {})


@pytest.mark.parametrize("problem", ["down", 500, 503])
async def test_3_30_yookassa_unavailable(
    provider: YooKassaProvider, yookassa: FakeYooKassa, problem: str | int
) -> None:
    """3.30: ЮКасса недоступна — соединение, таймаут или ошибка на её стороне."""
    if problem == "down":
        yookassa.down = True
    else:
        assert isinstance(problem, int)
        yookassa.status_code = problem
    with pytest.raises(ProviderUnavailableError):
        await provider.create_invoice(_request())


async def test_wrong_keys_are_reported(http: httpx2.AsyncClient) -> None:
    """Ключи, которые ЮКасса не принимает, — понятная ошибка для админки."""
    credentials = YooKassaCredentials(shop_id="1", secret_key="wrong")  # noqa: S106 — неверный
    wrong = YooKassaProvider(http, credentials)
    with pytest.raises(ProviderAuthError):
        await wrong.check_credentials()


async def test_3_10_yookassa_cannot_cancel_unpaid_payment(provider: YooKassaProvider) -> None:
    """3.10, 3.38: неоплаченный платёж ЮКасса не отменяет — он отменяется в магазине."""
    invoice = await provider.create_invoice(_request())
    assert await provider.cancel_invoice(invoice.provider_payment_id) is False


# --- Через сценарий покупки ---


@pytest.fixture
async def client(db_session: AsyncSession) -> Client:
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
    await connect_yookassa(db_session)
    return await make_client(db_session, telegram_id=777)


@pytest.fixture
async def tariff(db_session: AsyncSession) -> Tariff:
    tariff = make_tariff()
    await add(db_session, tariff)
    return tariff


def _checkout(client: Client, tariff: Tariff) -> Checkout:
    return Checkout(
        client=client,
        purpose=PaymentPurpose.PURCHASE,
        tariff=tariff,
        subscription=None,
        shown_price=tariff.price,
        token="screen-1",  # noqa: S106 — токен экрана подтверждения, не секрет
        description="Магазин: Месяц, 30 дней",
        return_url=RETURN_URL,
    )


@pytest.fixture
async def shop(
    valid_env: dict[str, str], db_session: AsyncSession, providers: Providers
) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        shop.app.state.providers = providers
        yield shop


async def test_3_31_paid_yookassa_invoice_is_confirmed_by_webhook(
    shop: Shop, providers: Providers, yookassa: FakeYooKassa, client: Client, tariff: Tariff
) -> None:
    """3.31, 3.4, 3.28: счёт через ЮКассу; её уведомление об оплате, проверенное
    запросом к API, делает платёж оплаченным."""
    payment = await open_invoice(shop.session, providers, _checkout(client, tariff), now=NOW)
    assert payment.provider == YOOKASSA
    assert payment.provider_payment_id is not None
    yookassa.succeed(payment.provider_payment_id)

    response = await shop.http.post(
        f"/webhooks/payments/{YOOKASSA}", content=_notification(payment.provider_payment_id)
    )

    assert response.status_code == 200
    assert payment.state == PaymentState.PAID


async def test_3_30_unavailable_yookassa_creates_nothing(
    db_session: AsyncSession,
    providers: Providers,
    yookassa: FakeYooKassa,
    client: Client,
    tariff: Tariff,
) -> None:
    """3.30, 3.31: ЮКасса недоступна при создании счёта — ничего не создаётся."""
    yookassa.down = True
    with pytest.raises(PaymentUnavailableError):
        await open_invoice(db_session, providers, _checkout(client, tariff), now=NOW)
    assert await db_session.scalar(select(Payment)) is None
