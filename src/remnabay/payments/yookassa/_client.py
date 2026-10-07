"""Клиент API ЮКассы v3: создание платежа, статус, проверка ключей."""

import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from remnabay.payments._provider import (
    Invoice,
    InvoiceRequest,
    NotificationError,
    PaymentProvider,
    ProviderAuthError,
    ProviderError,
    ProviderPayment,
    ProviderStatus,
    ProviderUnavailableError,
)

YOOKASSA = "yookassa"
API_URL = "https://api.yookassa.ru/v3"
# Запрос к провайдеру при создании счёта ждёт клиент в боте — недолго
TIMEOUT = timedelta(seconds=10)
# Назначение платежа — не длиннее 128 символов (ограничение API)
DESCRIPTION_LIMIT = 128
# Отмена по сроку или магазином — клиенту не сообщается; остальные причины — отказ (3.12)
_EXPIRED_REASONS = frozenset({"expired_on_confirmation", "payment_expired", "canceled_by_merchant"})
_OBJECT = TypeAdapter(dict[str, Any])
# Ошибки на стороне ЮКассы — повторить позже
_SERVER_ERRORS = 500


class YooKassaCredentials(BaseModel):
    """Ключи из личного кабинета ЮКассы: идентификатор магазина и секретный ключ."""

    model_config = ConfigDict(str_strip_whitespace=True)

    shop_id: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^\d+$")]
    secret_key: Annotated[str, Field(min_length=1, max_length=256)]


class _Amount(BaseModel):
    value: Decimal
    currency: str


class _CancellationDetails(BaseModel):
    party: str | None = None
    reason: str | None = None


class _Confirmation(BaseModel):
    type: str
    confirmation_url: str | None = None


class _Payment(BaseModel):
    """Платёж в ответе API и в теле уведомления (только нужные поля)."""

    id: str
    status: Literal["pending", "waiting_for_capture", "succeeded", "canceled"]
    amount: _Amount
    confirmation: _Confirmation | None = None
    cancellation_details: _CancellationDetails | None = None


class _Notification(BaseModel):
    type: Literal["notification"]
    event: str
    object: dict[str, Any]


def _status(payment: _Payment) -> ProviderStatus:
    match payment.status:
        case "succeeded":
            return ProviderStatus.SUCCEEDED
        case "canceled":
            reason = payment.cancellation_details.reason if payment.cancellation_details else None
            if reason in _EXPIRED_REASONS:
                return ProviderStatus.EXPIRED
            return ProviderStatus.DECLINED
        case _:
            # waiting_for_capture не бывает: платежи создаются с автоматическим списанием
            return ProviderStatus.PENDING


def _provider_payment(payment: _Payment) -> ProviderPayment:
    # Сумма, которую заплатил клиент (amount), а не за вычетом комиссии (income_amount):
    # её магазин сверяет со счётом (3.39)
    return ProviderPayment(
        provider_payment_id=payment.id,
        status=_status(payment),
        amount=payment.amount.value,
        currency=payment.amount.currency,
    )


class YooKassaProvider:
    """Подключённая ЮКасса с ключами оператора."""

    code = YOOKASSA

    def __init__(self, http: httpx2.AsyncClient, credentials: YooKassaCredentials) -> None:
        self._http = http
        self._auth = httpx2.BasicAuth(credentials.shop_id, credentials.secret_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotence-Key": idempotency_key} if idempotency_key else {}
        try:
            response = await self._http.request(
                method, f"{API_URL}{path}", json=body, headers=headers, auth=self._auth
            )
        except (httpx2.TimeoutException, httpx2.ConnectError) as error:
            raise ProviderUnavailableError(f"ЮКасса недоступна: {error!r}") from error
        except httpx2.HTTPError as error:
            raise ProviderUnavailableError(f"Сбой запроса к ЮКассе: {error!r}") from error
        if response.status_code in (401, 403):
            raise ProviderAuthError("ЮКасса не приняла идентификатор магазина или секретный ключ")
        if response.status_code >= _SERVER_ERRORS or response.status_code == 429:
            raise ProviderUnavailableError(f"ЮКасса ответила {response.status_code}")
        try:
            data: object = response.json()
        except ValueError as error:
            raise ProviderError(f"Непонятный ответ ЮКассы: {response.status_code}") from error
        if response.is_error or not isinstance(data, dict):
            raise ProviderError(f"ЮКасса ответила {response.status_code}: {data}")
        return _OBJECT.validate_python(data)

    async def check_credentials(self) -> None:
        """Ключи действуют: ЮКасса отвечает на запрос настроек магазина."""
        await self._request("GET", "/me")

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        body = {
            "amount": {"value": f"{request.amount:.2f}", "currency": request.currency},
            # Списание сразу, без двухэтапной оплаты (решение 0056)
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": request.return_url},
            "description": request.description[:DESCRIPTION_LIMIT],
            "metadata": {"payment_id": str(request.payment_id)},
        }
        data = await self._request(
            "POST", "/payments", body=body, idempotency_key=request.idempotency_key
        )
        try:
            payment = _Payment.model_validate(data)
        except ValidationError as error:
            raise ProviderError(f"Непонятный ответ ЮКассы на создание платежа: {error}") from error
        if payment.confirmation is None or payment.confirmation.confirmation_url is None:
            raise ProviderError("ЮКасса не вернула ссылку на оплату")
        return Invoice(payment.id, payment.confirmation.confirmation_url)

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        data = await self._request("GET", f"/payments/{provider_payment_id}")
        try:
            return _provider_payment(_Payment.model_validate(data))
        except ValidationError as error:
            raise ProviderError(f"Непонятный ответ ЮКассы о платеже: {error}") from error

    async def cancel_invoice(self, provider_payment_id: str) -> bool:
        """ЮКасса отменяет только платёж, ждущий подтверждения списания; неоплаченный
        платёж с автоматическим списанием отменить нельзя — он истечёт сам."""
        del provider_payment_id
        return False

    async def verify_notification(self, body: bytes, headers: Mapping[str, str]) -> ProviderPayment:
        """Уведомление не подписано: магазин запрашивает статус платежа у ЮКассы сам."""
        del headers
        try:
            notification = _Notification.model_validate(json.loads(body))
            payment_id = notification.object["id"]
        except (ValueError, ValidationError, KeyError, TypeError) as error:
            raise NotificationError("Уведомление не похоже на уведомление ЮКассы") from error
        if not isinstance(payment_id, str) or not payment_id:
            raise NotificationError("В уведомлении нет идентификатора платежа")
        try:
            return await self.fetch_payment(payment_id)
        except ProviderAuthError:
            raise
        except ProviderUnavailableError:
            raise
        except ProviderError as error:
            raise NotificationError(f"ЮКасса не подтвердила платёж {payment_id}") from error


class YooKassaKind:
    """ЮКасса как вид провайдера: ключи и подключение."""

    code = YOOKASSA
    title = "ЮКасса"
    # Счёт — в рублях: валюта учёта магазина по умолчанию (0020)
    currencies = frozenset({"RUB"})
    credentials_model: type[BaseModel] = YooKassaCredentials

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    def connect(self, credentials: BaseModel) -> PaymentProvider:
        if not isinstance(credentials, YooKassaCredentials):
            raise TypeError("Ключи не ЮКассы")
        return YooKassaProvider(self._http, credentials)


def yookassa_http(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """HTTP-клиент для ЮКассы: один на процесс, закрывается при остановке."""
    return httpx2.AsyncClient(
        timeout=TIMEOUT.total_seconds(),
        headers={"User-Agent": "RemnaBay"},
        transport=transport,
    )
