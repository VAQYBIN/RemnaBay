"""Контракт платёжного провайдера (3.27, 3.28, 3.33, решения 0020, 0056).

Сценарии покупки и продления работают с провайдером только через этот контракт:
создать счёт, проверить подтверждение оплаты, запросить статус счёта, отменить
счёт и сообщить, в каких валютах провайдер принимает счёт. Новый провайдер —
новая реализация контракта, сценарии не меняются.

Сверка подтверждённой суммы со счётом — часть контракта (3.39): провайдер
сообщает сумму и валюту, которые он подтвердил, а не сумму счёта, и магазин
сравнивает их сам (`confirm_payment`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class ProviderError(Exception):
    """Провайдер не выполнил запрос."""


class ProviderUnavailableError(ProviderError):
    """До провайдера не достучаться: соединение, таймаут, ошибка на его стороне (3.30)."""


class ProviderAuthError(ProviderError):
    """Провайдер не принял ключи оператора."""


class NotificationError(ProviderError):
    """Подтверждение не проверено по правилам провайдера (3.28)."""


class NotificationIgnoredError(ProviderError):
    """Уведомление не об оплате счёта (например, о возврате): магазину нечего делать,
    провайдер получает «принято», чтобы не повторять доставку."""


class ProviderStatus(StrEnum):
    """Состояние счёта у провайдера, как его понимает магазин."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    # Провайдер отказал в оплате (3.12)
    DECLINED = "declined"
    # Счёт закрыт без оплаты по сроку или отменён — клиенту ничего не сообщается
    EXPIRED = "expired"


@dataclass(frozen=True)
class InvoiceRequest:
    """Что магазин просит у провайдера: счёт на сумму в валюте учёта (3.33)."""

    payment_id: int
    amount: Decimal
    currency: str
    # Назначение платежа на странице провайдера (`payment.description`, решение 0057)
    description: str
    # Повтор запроса с тем же ключом не создаёт второй счёт (3.5)
    idempotency_key: str
    # Куда вернуть клиента после оплаты — в бот (3.32)
    return_url: str


@dataclass(frozen=True)
class Invoice:
    provider_payment_id: str
    # Страница оплаты, куда переходит клиент (3.4)
    payment_url: str


@dataclass(frozen=True)
class ProviderPayment:
    """Счёт по данным провайдера. Сумма и валюта — подтверждённые провайдером (3.39)."""

    provider_payment_id: str
    status: ProviderStatus
    amount: Decimal
    currency: str


class PaymentProvider(Protocol):
    """Подключённый провайдер с ключами оператора."""

    @property
    def code(self) -> str:
        """Код провайдера: в платеже, журнале и адресе вебхука."""
        ...

    async def check_credentials(self) -> None:
        """Ключи действуют; иначе `ProviderAuthError`."""
        ...

    async def create_invoice(self, request: InvoiceRequest) -> Invoice: ...

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        """Статус счёта — для опроса неоплаченных счетов (3.29)."""
        ...

    async def cancel_invoice(self, provider_payment_id: str) -> bool:
        """Отменяет неоплаченный счёт. `False` — провайдер такой отмены не умеет (3.10, 3.38)."""
        ...

    async def verify_notification(self, body: bytes, headers: Mapping[str, str]) -> ProviderPayment:
        """Проверяет уведомление по правилам провайдера и возвращает проверенный счёт.

        Не проверено — `NotificationError` (3.28)."""
        ...


class ProviderKind(Protocol):
    """Вид провайдера: какие ключи нужны и как из них получить подключённого провайдера."""

    @property
    def code(self) -> str: ...

    @property
    def title(self) -> str:
        """Название в админке."""
        ...

    @property
    def currencies(self) -> frozenset[str]:
        """Валюты, в которых провайдер принимает счёт (3.33): известны до подключения."""
        ...

    @property
    def credentials_model(self) -> type[BaseModel]:
        """Ключи, которые оператор вводит в админке (Pydantic, 0022)."""
        ...

    def connect(self, credentials: BaseModel) -> PaymentProvider:
        """Провайдер с ключами оператора; ключи — экземпляр `credentials_model`."""
        ...
