"""Поддельный платёжный провайдер для тестов: реализует контракт провайдера (3.27)."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

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
