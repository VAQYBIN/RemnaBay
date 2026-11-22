"""Приём обновлений бота вебхуком (0026, 0051).

Telegram присылает секрет, заданный при регистрации вебхука; без него — отказ.
Ответ «принято» уходит и при ошибке в обработчике: иначе Telegram прислал бы
обновление снова, и клиент мог бы получить ответ дважды (сквозное правило 4).
"""

import hmac
import logging
from typing import Annotated

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot import TELEGRAM_WEBHOOK_PATH
from remnabay.crypto import SecretBox, SecretDecryptionError
from remnabay.shop_settings import TELEGRAM_WEBHOOK_SECRET, generated_secret
from remnabay.web.deps import get_box, get_session

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 — имя заголовка

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(TELEGRAM_WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    box: Annotated[SecretBox, Depends(get_box)],
) -> Response:
    try:
        secret = await generated_secret(session, box, TELEGRAM_WEBHOOK_SECRET)
        await session.commit()
    except SecretDecryptionError:
        logger.error("Секрет вебхука бота не расшифровывается: сменили ENCRYPTION_KEY?")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    received = request.headers.get(SECRET_HEADER, "")
    if not hmac.compare_digest(received.encode(), secret.encode()):
        logger.warning("Обновление бота без верного секрета отклонено")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    bot: Bot = request.app.state.bot
    dispatcher: Dispatcher = request.app.state.dispatcher
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except ValueError, ValidationError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    try:
        await dispatcher.feed_update(bot, update)
    except Exception:
        logger.exception("Ошибка при обработке обновления бота %s", update.update_id)
    return Response(status_code=status.HTTP_200_OK)
