"""Поддельные платёжные провайдеры для тестов: контракт провайдера в памяти (3.27) и
поддельный API ЮКассы."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox
from remnabay.payments import (
    Invoice,
    InvoiceRequest,
    NotificationError,
    PaymentProvider,
    ProviderPayment,
    Providers,
    ProviderStatus,
    ProviderUnavailableError,
)
from remnabay.payments.yookassa import YOOKASSA, YooKassaCredentials, yookassa_http
from remnabay.shop_settings import ShopSettingValue
from tests.conftest import REQUIRED_ENV

FAKE = "fake"
# Заголовок, которым поддельный провайдер «подписывает» уведомление
SIGNATURE = "x-fake-signature"
VALID_SIGNATURE = "ok"


class FakeCredentials(BaseModel):
    account: str


@dataclass
class FakeProvider:
    """Провайдер в памяти: счета, их статусы и отмены; может быть недоступен."""

    code: str = FAKE
    payments: dict[str, ProviderPayment] = field(default_factory=dict[str, ProviderPayment])
    requests: list[InvoiceRequest] = field(default_factory=list[InvoiceRequest])
    cancelled: list[str] = field(default_factory=list[str])
    can_cancel: bool = True
    unavailable: bool = False

    async def check_credentials(self) -> None:
        if self.unavailable:
            raise ProviderUnavailableError("Провайдер недоступен")

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        if self.unavailable:
            raise ProviderUnavailableError("Провайдер недоступен")
        self.requests.append(request)
        payment_id = f"fp-{request.idempotency_key}"
        self.payments.setdefault(
            payment_id,
            ProviderPayment(payment_id, ProviderStatus.PENDING, request.amount, request.currency),
        )
        return Invoice(payment_id, f"https://pay.example/{payment_id}")

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        if self.unavailable:
            raise ProviderUnavailableError("Провайдер недоступен")
        return self.payments[provider_payment_id]

    async def cancel_invoice(self, provider_payment_id: str) -> bool:
        if not self.can_cancel:
            return False
        self.cancelled.append(provider_payment_id)
        self.set(provider_payment_id, ProviderStatus.EXPIRED)
        return True

    async def verify_notification(self, body: bytes, headers: Mapping[str, str]) -> ProviderPayment:
        if headers.get(SIGNATURE) != VALID_SIGNATURE:
            raise NotificationError("Подпись не сошлась")
        payment_id = json.loads(body)["id"]
        if payment_id not in self.payments:
            raise NotificationError("Счёт не найден у провайдера")
        return await self.fetch_payment(payment_id)

    def set(
        self, provider_payment_id: str, status: ProviderStatus, amount: Decimal | None = None
    ) -> ProviderPayment:
        """Клиент оплатил, получил отказ или счёт закрылся — на стороне провайдера."""
        old = self.payments[provider_payment_id]
        new = ProviderPayment(
            provider_payment_id, status, old.amount if amount is None else amount, old.currency
        )
        self.payments[provider_payment_id] = new
        return new


@dataclass
class FakeKind:
    """Вид поддельного провайдера: все подключения ведут к одному провайдеру в памяти."""

    provider: FakeProvider = field(default_factory=FakeProvider)
    code: str = FAKE
    title: str = "Тестовый провайдер"
    currencies: frozenset[str] = frozenset({"RUB"})
    credentials_model: type[BaseModel] = FakeCredentials

    def connect(self, credentials: BaseModel) -> PaymentProvider:
        assert isinstance(credentials, FakeCredentials)
        return self.provider


def shop_box() -> SecretBox:
    return SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])


def fake_providers(kind: FakeKind | None = None) -> Providers:
    return Providers(shop_box(), [kind or FakeKind()])


async def connect_fake(session: AsyncSession, box: SecretBox | None = None) -> None:
    """Ключи поддельного провайдера сохранены, будто их ввёл оператор."""
    stored = (box or shop_box()).encrypt(FakeCredentials(account="test").model_dump_json())
    await session.merge(ShopSettingValue(key=f"payments.{FAKE}.credentials", value=stored))
    await session.flush()


# --- Поддельный API ЮКассы ---

YOOKASSA_SHOP_ID = "123456"
YOOKASSA_SECRET = "test_secret"  # noqa: S105 — ключ поддельной ЮКассы


@dataclass
class FakeYooKassa:
    """API ЮКассы v3 в памяти: платежи, идемпотентность, проверка ключей."""

    payments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    by_key: dict[str, str] = field(default_factory=dict[str, str])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    down: bool = False
    status_code: int | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.down:
            raise httpx2.ConnectError("ЮКасса недоступна", request=request)
        if self.status_code is not None:
            return httpx2.Response(self.status_code, json={"type": "error", "code": "x"})
        expected = httpx2.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET)
        auth_header = next(expected.auth_flow(httpx2.Request("GET", "https://x"))).headers[
            "Authorization"
        ]
        if request.headers.get("Authorization") != auth_header:
            return httpx2.Response(401, json={"type": "error", "code": "invalid_credentials"})
        path = request.url.path
        if path == "/v3/me":
            return httpx2.Response(200, json={"account_id": YOOKASSA_SHOP_ID, "test": True})
        if path == "/v3/payments" and request.method == "POST":
            key = request.headers["Idempotence-Key"]
            if key not in self.by_key:
                body = json.loads(request.content)
                payment_id = f"yk-{len(self.payments) + 1}"
                self.payments[payment_id] = {
                    "id": payment_id,
                    "status": "pending",
                    "amount": body["amount"],
                    "description": body.get("description"),
                    "metadata": body.get("metadata"),
                    "confirmation": {
                        "type": "redirect",
                        "confirmation_url": f"https://yoomoney.example/pay/{payment_id}",
                        "return_url": body["confirmation"]["return_url"],
                    },
                    "paid": False,
                    "test": True,
                }
                self.by_key[key] = payment_id
            return httpx2.Response(200, json=self.payments[self.by_key[key]])
        if path.startswith("/v3/payments/") and request.method == "GET":
            payment = self.payments.get(path.removeprefix("/v3/payments/"))
            if payment is None:
                return httpx2.Response(404, json={"type": "error", "code": "not_found"})
            return httpx2.Response(200, json=payment)
        return httpx2.Response(404, json={"type": "error", "code": "not_found"})

    def succeed(self, payment_id: str, amount: str | None = None) -> None:
        payment = self.payments[payment_id]
        payment["status"] = "succeeded"
        payment["paid"] = True
        if amount is not None:
            payment["amount"] = {**payment["amount"], "value": amount}

    def cancel(self, payment_id: str, reason: str) -> None:
        self.payments[payment_id]["status"] = "canceled"
        self.payments[payment_id]["cancellation_details"] = {
            "party": "yoo_kassa",
            "reason": reason,
        }

    def http(self) -> httpx2.AsyncClient:
        return yookassa_http(httpx2.MockTransport(self.handle))


async def connect_yookassa(session: AsyncSession, box: SecretBox | None = None) -> None:
    """Ключи ЮКассы сохранены, будто их ввёл оператор."""
    credentials = YooKassaCredentials(shop_id=YOOKASSA_SHOP_ID, secret_key=YOOKASSA_SECRET)
    stored = (box or shop_box()).encrypt(credentials.model_dump_json())
    await session.merge(ShopSettingValue(key=f"payments.{YOOKASSA}.credentials", value=stored))
    await session.flush()
