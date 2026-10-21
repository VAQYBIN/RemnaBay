"""«Настройки → Оплата»: провайдеры и время жизни счёта (3.31, 3.33, 3.42, 1.14, 0049).

Ключи провайдера проверяются у провайдера до сохранения и хранятся зашифрованными
(0046); админка ключи не показывает. Провайдер, который не принимает валюту учёта,
можно подключить, но клиенту он недоступен — админка это показывает (3.33).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from remnabay.payments import (
    INVOICE_LIFETIME,
    ConnectionState,
    ProviderAuthError,
    ProviderError,
    ProviderKeysError,
    Providers,
    ProviderUnavailableError,
    payable_invoices,
)
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import get_setting, set_setting, shop_time_zone
from remnabay.web.admin._deps import AppSettings, DbSession, Owner
from remnabay.web.payment_webhook import payment_webhook_path

router = APIRouter(tags=["settings"])


class ProviderOut(BaseModel):
    code: str
    title: str
    state: ConnectionState
    # Валюты, в которых провайдер принимает счёт (3.33)
    currencies: list[str]
    # Адрес для уведомлений — оператор указывает его в кабинете провайдера
    webhook_url: str


class PaymentSettingsOut(BaseModel):
    providers: list[ProviderOut]
    currency: str
    invoice_lifetime_minutes: int


class PaymentSettingsIn(BaseModel):
    # От 5 минут до суток
    invoice_lifetime_minutes: Annotated[int, Field(ge=5, le=1440)]


class ProviderKeysIn(BaseModel):
    credentials: dict[str, str]


class ProviderRejectedOut(BaseModel):
    reason: Literal["invalid_keys", "rejected"]
    message: str


class PayableInvoicesOut(BaseModel):
    """У клиентов есть счета провайдера, которые ещё могут оплатить (3.42)."""

    reason: Literal["payable_invoices"] = "payable_invoices"
    message: str
    count: int
    # Когда закончится окно опроса последнего из счетов; показывается в часовом поясе магазина
    window_ends_at: datetime
    time_zone: str


async def _payment_settings(
    session: DbSession, settings: AppSettings, providers: Providers
) -> PaymentSettingsOut:
    connections = await providers.connections(session)
    lifetime = await get_setting(session, INVOICE_LIFETIME)
    await session.commit()
    return PaymentSettingsOut(
        providers=[
            ProviderOut(
                code=connection.kind.code,
                title=connection.kind.title,
                state=connection.state,
                currencies=sorted(connection.kind.currencies),
                webhook_url=settings.public_link(payment_webhook_path(connection.kind.code)),
            )
            for connection in connections
        ],
        currency=await get_setting(session, SHOP_CURRENCY),
        invoice_lifetime_minutes=int(lifetime.total_seconds() // 60),
    )


def _app_providers(request: Request) -> Providers:
    providers: Providers = request.app.state.providers
    return providers


@router.get("/settings/payment")
async def payment_settings(
    request: Request, session: DbSession, settings: AppSettings, owner: Owner
) -> PaymentSettingsOut:
    del owner
    return await _payment_settings(session, settings, _app_providers(request))


@router.put("/settings/payment")
async def update_payment_settings(
    body: PaymentSettingsIn,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    owner: Owner,
) -> PaymentSettingsOut:
    """Время жизни счёта (3.8)."""
    lifetime = timedelta(minutes=body.invoice_lifetime_minutes)
    if await get_setting(session, INVOICE_LIFETIME) != lifetime:
        await set_setting(session, INVOICE_LIFETIME, lifetime, member_id=owner.id)
    await session.commit()
    return await _payment_settings(session, settings, _app_providers(request))


@router.put(
    "/settings/payment/providers/{code}",
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_409_CONFLICT: {"model": ProviderRejectedOut},
    },
)
async def connect_provider(
    code: str,
    body: ProviderKeysIn,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    owner: Owner,
) -> PaymentSettingsOut:
    """Подключить провайдера: ключи проверяются у провайдера, затем сохраняются."""
    providers = _app_providers(request)
    kind = providers.kind(code)
    if kind is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого провайдера нет")
    try:
        credentials = providers.parse_credentials(kind, body.credentials)
        await kind.connect(credentials).check_credentials()
    except ProviderKeysError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ProviderRejectedOut(reason="invalid_keys", message=str(error)).model_dump(),
        ) from error
    except ProviderAuthError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ProviderRejectedOut(reason="rejected", message=str(error)).model_dump(),
        ) from error
    except ProviderUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Провайдер не отвечает — попробуйте позже",
        ) from error
    except ProviderError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            ProviderRejectedOut(reason="rejected", message=str(error)).model_dump(),
        ) from error
    await providers.save(session, kind, credentials, member_id=owner.id)
    await session.commit()
    return await _payment_settings(session, settings, providers)


@router.delete(
    "/settings/payment/providers/{code}",
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_409_CONFLICT: {"model": PayableInvoicesOut},
    },
)
async def disconnect_provider(
    code: str,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    owner: Owner,
    confirm_payable: bool = False,
) -> PaymentSettingsOut:
    """Отключить провайдера: новые счета через него не создаются. Уже созданные счета
    опрашиваются, пока ключи есть, — без ключей оплату найдёт команда.

    Если клиенты ещё могут оплатить счета провайдера, без `confirm_payable` — отказ
    с их числом: владелец должен увидеть предупреждение (3.42).
    """
    providers = _app_providers(request)
    kind = providers.kind(code)
    if kind is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого провайдера нет")
    payable = await payable_invoices(session, kind.code, now=datetime.now(UTC))
    if payable.window_ends_at is not None and not confirm_payable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            PayableInvoicesOut(
                message="У клиентов есть счета, которые ещё могут быть оплачены",
                count=payable.count,
                window_ends_at=payable.window_ends_at,
                time_zone=(await shop_time_zone(session)).key,
            ).model_dump(mode="json"),
        )
    await providers.disconnect(session, kind, member_id=owner.id, payable_invoices=payable.count)
    await session.commit()
    return await _payment_settings(session, settings, providers)
