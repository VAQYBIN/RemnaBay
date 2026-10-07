"""Приём уведомлений платёжных провайдеров (3.28).

Уведомление проверяется по правилам своего провайдера (контракт провайдера,
3.27): непроверенное отклоняется и пишется в журнал. Ответ «принято» уходит
только после того, как подтверждение сохранено, — иначе провайдер перестал бы
повторять доставку. Если уведомление потеряется, оплату найдёт опрос статуса
счетов (3.29).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.payments import (
    NotificationError,
    NotificationIgnoredError,
    ProviderError,
    Providers,
    provider_reported,
)
from remnabay.web.deps import get_providers, get_session

PAYMENT_WEBHOOK_PATH = "/webhooks/payments/{provider_code}"
# Уведомления провайдеров — небольшие JSON
MAX_BODY_BYTES = 256 * 1024

logger = logging.getLogger(__name__)
router = APIRouter()


def payment_webhook_path(provider_code: str) -> str:
    """Адрес вебхука провайдера — его оператор указывает в кабинете провайдера."""
    return PAYMENT_WEBHOOK_PATH.format(provider_code=provider_code)


async def _read_body(request: Request) -> bytes | None:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _reject(
    session: AsyncSession, provider_code: str, reason: str, status_code: int, error: str = ""
) -> Response:
    """Отправитель не подтверждён — в журнал от имени системы, как у вебхука панели (0048)."""
    details: dict[str, JsonValue] = {"reason": reason, "provider": provider_code}
    if error:
        details["error"] = error
    await record(
        session,
        actor=Actor.SYSTEM,
        action="payment.notification_rejected",
        outcome=Outcome.FAILURE,
        subject=Subject("payment_provider", provider_code),
        details=details,
    )
    await session.commit()
    return Response(status_code=status_code)


@router.post(PAYMENT_WEBHOOK_PATH, include_in_schema=False)
async def payment_webhook(
    provider_code: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    providers: Annotated[Providers, Depends(get_providers)],
) -> Response:
    if providers.kind(provider_code) is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    body = await _read_body(request)
    if body is None:
        return await _reject(session, provider_code, "too_large", status.HTTP_413_CONTENT_TOO_LARGE)
    provider = await providers.get(session, provider_code)
    if provider is None:
        # Провайдер не подключён или ключи не расшифровываются: проверить нечем
        return await _reject(
            session, provider_code, "not_connected", status.HTTP_503_SERVICE_UNAVAILABLE
        )
    try:
        reported = await provider.verify_notification(body, request.headers)
    except NotificationIgnoredError:
        return Response(status_code=status.HTTP_200_OK)
    except NotificationError as error:
        return await _reject(
            session, provider_code, "not_verified", status.HTTP_400_BAD_REQUEST, str(error)
        )
    except ProviderError as error:
        # Провайдер не ответил на проверку — пусть повторит уведомление позже
        logger.warning("Уведомление %s не проверено: %s", provider_code, error)
        return await _reject(
            session, provider_code, "unavailable", status.HTTP_503_SERVICE_UNAVAILABLE, str(error)
        )
    await provider_reported(session, provider_code, reported)
    await session.commit()
    return Response(status_code=status.HTTP_200_OK)
