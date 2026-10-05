"""Приём вебхуков панели (4.1, 4.2).

Подпись проверяется всегда — даже если панель шлёт событие по внутренней
Docker-сети (0026). Неверная или отсутствующая подпись — отказ и запись в
журнал. Ответ «принято» уходит только после того, как событие сохранено:
иначе панель перестала бы повторять доставку, а событие потерялось бы
(его всё равно нашла бы сверка, 4.8, но позже).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox, SecretDecryptionError
from remnabay.journal import Actor, Outcome, record
from remnabay.panel import SIGNATURE_HEADER, PanelResponseError, parse_event, verify_signature
from remnabay.panel_sync import receive_event
from remnabay.shop_settings import webhook_secret
from remnabay.web.deps import get_box, get_session

PANEL_WEBHOOK_PATH = "/webhooks/panel"
# События панели — небольшие JSON; больше — не от панели
MAX_BODY_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)
router = APIRouter()


async def _reject(session: AsyncSession, reason: str, status_code: int) -> Response:
    await record(
        session,
        actor=Actor.SYSTEM,
        action="panel.webhook_rejected",
        outcome=Outcome.FAILURE,
        details={"reason": reason},
    )
    await session.commit()
    return Response(status_code=status_code)


@router.post(PANEL_WEBHOOK_PATH, include_in_schema=False)
async def panel_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    box: Annotated[SecretBox, Depends(get_box)],
) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return await _reject(session, "too_large", status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        secret = await webhook_secret(session, box)
    except SecretDecryptionError:
        logger.error("Секрет вебхука панели не расшифровывается: сменили ENCRYPTION_KEY?")
        return await _reject(session, "secret_unreadable", status.HTTP_503_SERVICE_UNAVAILABLE)
    if not verify_signature(body, request.headers.get(SIGNATURE_HEADER), secret):
        return await _reject(session, "signature", status.HTTP_401_UNAUTHORIZED)
    try:
        event = parse_event(body)
    except PanelResponseError:
        return await _reject(session, "unreadable", status.HTTP_400_BAD_REQUEST)
    await receive_event(session, body, event)
    await session.commit()
    return Response(status_code=status.HTTP_200_OK)
