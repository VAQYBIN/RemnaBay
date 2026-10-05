"""Бот в роли «веб» (0026, 0051): вебхук в рабочем режиме, опрос при разработке."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import AiogramError

from remnabay.bot import _fallback
from remnabay.bot._context import SESSIONS_KEY, SessionFactory, context_middleware

TELEGRAM_WEBHOOK_PATH = "/webhooks/telegram"
# Как у отправителя сообщений воркера
REQUEST_TIMEOUT = timedelta(seconds=10)

logger = logging.getLogger(__name__)


def create_bot(token: str, *, session: BaseSession | None = None) -> Bot:
    return Bot(token, session=session or AiohttpSession(timeout=REQUEST_TIMEOUT.total_seconds()))


def create_dispatcher(sessions: SessionFactory) -> Dispatcher:
    """Диспетчер со всеми роутерами; непонятное сообщение — последним."""
    dispatcher = Dispatcher()
    dispatcher.workflow_data[SESSIONS_KEY] = sessions
    dispatcher.update.outer_middleware(context_middleware)
    dispatcher.include_router(_fallback.build_router())
    return dispatcher


async def register_webhook(bot: Bot, dispatcher: Dispatcher, url: str, secret: str) -> None:
    """Регистрирует вебхук бота. Ошибка не мешает запуску магазина: прежний вебхук,
    если он был, продолжает работать, а причина видна в логе."""
    try:
        await bot.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    except AiogramError:
        logger.exception("Не удалось зарегистрировать вебхук бота %s", url)
    else:
        logger.info("Вебхук бота зарегистрирован: %s", url)


class Polling:
    """Опрос Telegram при разработке: публичный адрес не нужен (0051)."""

    def __init__(self, bot: Bot, dispatcher: Dispatcher) -> None:
        self._bot = bot
        self._dispatcher = dispatcher
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Пока зарегистрирован вебхук, Telegram отказывает в опросе
        await self._bot.delete_webhook()
        # У параметра allowed_updates в aiogram тип с неразобранным UNSET
        polling = self._dispatcher.start_polling(  # pyright: ignore[reportUnknownMemberType]
            self._bot, handle_signals=False, close_bot_session=False
        )
        self._task = asyncio.create_task(polling)

    async def stop(self) -> None:
        if self._task is None:
            return
        with suppress(RuntimeError):
            # Опрос ещё не успел начаться — останавливать нечего
            await self._dispatcher.stop_polling()
        await self._task
