"""Бот в роли «веб» (0026, 0051): вебхук в рабочем режиме, опрос при разработке."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import AiogramError, TelegramAPIError, TelegramNetworkError

from remnabay.bot import _devices, _fallback, _guide, _help, _login, _promo, _shop
from remnabay.bot._context import (
    PANEL_KEY,
    SESSIONS_KEY,
    SHOP_URL_KEY,
    SessionFactory,
    context_middleware,
)
from remnabay.messaging import TelegramSession
from remnabay.panel import PanelClient
from remnabay.payments import Providers

TELEGRAM_WEBHOOK_PATH = "/webhooks/telegram"
# Как у отправителя сообщений воркера
REQUEST_TIMEOUT = timedelta(seconds=10)

logger = logging.getLogger(__name__)


def create_bot(token: str, *, proxy: str | None = None, session: BaseSession | None = None) -> Bot:
    """Бот веба. `proxy` — прокси для запросов к Telegram (1.28, 0074)."""
    if session is None:
        session = TelegramSession(timeout=REQUEST_TIMEOUT, proxy=proxy)
    return Bot(token, session=session)


def create_dispatcher(
    sessions: SessionFactory,
    *,
    admin_login_url: str,
    providers: Providers,
    panel: PanelClient,
    shop_url: str,
) -> Dispatcher:
    """Диспетчер со всеми роутерами; непонятное сообщение — последним.

    `admin_login_url` — страница входа в админку для кнопки на /admin;
    `providers` — платёжные провайдеры для оплаты (3.27); `panel` — клиент панели для
    инструкции и устройств; `shop_url` — публичный адрес магазина (страница-переходник).
    """
    dispatcher = Dispatcher()
    dispatcher.workflow_data[SESSIONS_KEY] = sessions
    dispatcher.workflow_data[_login.ADMIN_LOGIN_URL_KEY] = admin_login_url
    dispatcher.workflow_data[_shop.PROVIDERS_KEY] = providers
    dispatcher.workflow_data[PANEL_KEY] = panel
    dispatcher.workflow_data[SHOP_URL_KEY] = shop_url.rstrip("/")
    dispatcher.update.outer_middleware(context_middleware)
    dispatcher.include_router(_login.build_router())
    dispatcher.include_router(_shop.build_router())
    dispatcher.include_router(_guide.build_router())
    dispatcher.include_router(_devices.build_router())
    dispatcher.include_router(_help.build_router())
    dispatcher.include_router(_promo.build_router())
    dispatcher.include_router(_fallback.build_router())
    return dispatcher


class WebhookProblem(StrEnum):
    # Запрос не дошёл до Telegram: нет связи с сервера магазина или не работает прокси
    NETWORK = "network"
    # Telegram ответил отказом (например, адрес магазина не HTTPS)
    REJECTED = "rejected"


@dataclass
class BotWebhookStatus:
    """Вебхук бота в этом процессе веба — для чек-листа.

    `polling` — при разработке бот опрашивает Telegram, вебхук не нужен (0051).
    `message` — ответ Telegram при отказе.
    """

    registered: bool = False
    polling: bool = False
    problem: WebhookProblem | None = None
    message: str | None = None


@dataclass(frozen=True)
class WebhookRetry:
    """Паузы между попытками регистрации: от `first`, вдвое дольше, не больше `longest`."""

    first: timedelta = timedelta(seconds=10)
    longest: timedelta = timedelta(minutes=5)


def _problem(error: AiogramError) -> tuple[WebhookProblem, str | None]:
    if isinstance(error, TelegramNetworkError):
        return WebhookProblem.NETWORK, None
    if isinstance(error, TelegramAPIError):
        return WebhookProblem.REJECTED, error.message
    return WebhookProblem.REJECTED, str(error)


async def register_webhook(
    bot: Bot,
    dispatcher: Dispatcher,
    url: str,
    secret: str,
    status: BotWebhookStatus,
    retry: WebhookRetry = WebhookRetry(),  # noqa: B008 — неизменяемое значение по умолчанию
) -> None:
    """Регистрирует вебхук бота; при ошибке повторяет, пока не получится.

    Запускается фоновой задачей: магазин не ждёт Telegram при запуске. Прежний
    вебхук, если он был, тем временем продолжает работать. Если связи с Telegram не
    было (например, прокси заработал позже), вебхук регистрируется без перезапуска
    магазина, а пока — пункт «Вебхук бота» в чек-листе называет причину.
    """
    delay = retry.first
    while True:
        try:
            await bot.set_webhook(
                url=url,
                secret_token=secret,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        except AiogramError as error:
            status.problem, status.message = _problem(error)
            # Одна строка без трассировки: при долгом отсутствии связи попытки повторяются
            logger.warning(
                "Не удалось зарегистрировать вебхук бота %s (%s: %s), повтор через %.0f с",
                url,
                type(error).__name__,
                error,
                delay.total_seconds(),
            )
        else:
            status.registered, status.problem, status.message = True, None, None
            logger.info("Вебхук бота зарегистрирован: %s", url)
            return
        await asyncio.sleep(delay.total_seconds())
        delay = min(delay * 2, retry.longest)


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
