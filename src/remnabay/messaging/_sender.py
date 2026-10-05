"""Отправка сообщения в Telegram и разбор того, чем она закончилась.

Сквозное правило 4 и критерий 4.33: сообщение отправляется не больше одного раза.
Поэтому важен не сам сбой, а то, принял ли Telegram сообщение:
- точно не принял (слишком много запросов, соединение не установлено, Telegram
  перезапускается, неверный токен) — повтор безопасен;
- неизвестно (таймаут, обрыв соединения, ошибка сервера Telegram, непонятный
  ответ) — повтора нет: лучше потерять сообщение, чем отправить дважды;
- отказ по существу (клиент заблокировал бот, чат не найден, неверное
  сообщение) — повтор не поможет.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.exceptions import (
    AiogramError,
    RestartingTelegram,
    TelegramAPIError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import ClientConnectorError

# Меньше таймаута задачи очереди (30 с): иначе зависшую отправку прервала бы очередь,
# и магазин не узнал бы, что случилось с сообщением
DEFAULT_TIMEOUT = timedelta(seconds=10)


class DeliveryError(Exception):
    """Сообщение не доставлено."""


class RecipientBlockedError(DeliveryError):
    """Получатель заблокировал бот или удалил аккаунт (ответ 403)."""


class NotAcceptedError(DeliveryError):
    """Telegram точно не принял сообщение — повтор безопасен (4.33)."""


class UndeliverableError(DeliveryError):
    """Telegram отказал по существу: чат не найден, неверное сообщение — повтор не поможет."""


class AmbiguousDeliveryError(DeliveryError):
    """Неизвестно, принял ли Telegram сообщение — повторять нельзя (4.33)."""


@dataclass(frozen=True)
class Button:
    """Кнопка под сообщением: ссылка или копирование текста."""

    text: str
    url: str | None = None
    copy_text: str | None = None

    def __post_init__(self) -> None:
        if (self.url is None) == (self.copy_text is None):
            raise ValueError("У кнопки — либо ссылка, либо текст для копирования")


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    buttons: tuple[Button, ...] = ()


class Sender(Protocol):
    """Отправитель сообщений. Ошибки — подклассы `DeliveryError`."""

    async def send(self, chat_id: int, message: OutgoingMessage) -> None: ...


def _keyboard(buttons: tuple[Button, ...]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=button.text,
                url=button.url,
                copy_text=CopyTextButton(text=button.copy_text) if button.copy_text else None,
            )
        ]
        for button in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def classify(error: AiogramError) -> DeliveryError:
    """Чем закончилась отправка: по ошибке aiogram — вид недоставки."""
    match error:
        case TelegramForbiddenError():
            return RecipientBlockedError(str(error))
        case TelegramRetryAfter() | RestartingTelegram() | TelegramUnauthorizedError():
            return NotAcceptedError(str(error))
        case TelegramEntityTooLarge():
            # В aiogram это подкласс сетевой ошибки, но это ответ 413 — отказ по существу
            return UndeliverableError(str(error))
        case TelegramNetworkError() if isinstance(error.__cause__, ClientConnectorError):
            # Соединение не установлено — запрос до Telegram не дошёл
            return NotAcceptedError(str(error))
        case TelegramNetworkError() | TelegramServerError():
            return AmbiguousDeliveryError(str(error))
        case TelegramAPIError():
            # Остальные ответы Telegram с ошибкой (400, 404, 409…) — отказ по существу
            return UndeliverableError(str(error))
        case _:
            # Ответ не разобран — неизвестно, принято ли сообщение
            return AmbiguousDeliveryError(str(error))


class TelegramSender:
    """Отправитель через Bot API (aiogram). Только исходящие сообщения: бот — этап 4."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str | None = None,
        timeout: timedelta = DEFAULT_TIMEOUT,
    ) -> None:
        api = TelegramAPIServer.from_base(api_base) if api_base else PRODUCTION
        session = AiohttpSession(api=api, timeout=timeout.total_seconds())
        self._bot = Bot(token, session=session)

    async def send(self, chat_id: int, message: OutgoingMessage) -> None:
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=message.text,
                reply_markup=_keyboard(message.buttons),
            )
        except AiogramError as error:
            raise classify(error) from error

    async def close(self) -> None:
        await self._bot.session.close()
