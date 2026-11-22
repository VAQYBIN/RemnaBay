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

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, override

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
from aiogram.methods import TelegramMethod
from aiogram.types import (
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from aiohttp import ClientConnectorError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from remnabay.messaging._dates import DATE_FORMAT, DateEntity

# Меньше таймаута задачи очереди (30 с): иначе зависшую отправку прервала бы очередь,
# и магазин не узнал бы, что случилось с сообщением
DEFAULT_TIMEOUT = timedelta(seconds=10)
# Ответ 403, если человек ни разу не открывал бот: «bot can't initiate conversation with a user»
_NOT_STARTED = "can't initiate conversation"


class DeliveryError(Exception):
    """Сообщение не доставлено."""


class RecipientBlockedError(DeliveryError):
    """Получатель заблокировал бот или удалил аккаунт (ответ 403)."""


class RecipientNotStartedError(DeliveryError):
    """Получатель ни разу не открывал бот: Telegram не даёт боту написать первым (ответ 403).
    Это не блокировка — пометки «бот заблокирован» нет (решение 0069)."""


class NotAcceptedError(DeliveryError):
    """Telegram точно не принял сообщение — повтор безопасен (4.33)."""


class UndeliverableError(DeliveryError):
    """Telegram отказал по существу: чат не найден, неверное сообщение — повтор не поможет."""


class AmbiguousDeliveryError(DeliveryError):
    """Неизвестно, принял ли Telegram сообщение — повторять нельзя (4.33)."""


@dataclass(frozen=True)
class Button:
    """Кнопка под сообщением: ссылка, копирование текста или нажатие, которое
    разбирает бот. `style` — смысловой цвет: `success` у целевого действия (03-screens)."""

    text: str
    url: str | None = None
    copy_text: str | None = None
    callback_data: str | None = None
    style: str | None = None

    def __post_init__(self) -> None:
        kinds = (self.url, self.copy_text, self.callback_data)
        if sum(kind is not None for kind in kinds) != 1:
            raise ValueError("У кнопки — ровно одно: ссылка, текст для копирования или нажатие")


@dataclass(frozen=True)
class TextEntity:
    """Форматирование части текста: жирный, ссылка, цитата и т. п. (сущность Telegram).
    Смещение и длина — в единицах UTF-16."""

    type: str
    offset: int
    length: int
    url: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class Photo:
    """Изображение сообщения: файл, уже загруженный в Telegram (`file_id`), или данные
    для первой загрузки."""

    file_id: str | None = None
    data: bytes | None = None
    filename: str = "image"

    def __post_init__(self) -> None:
        if (self.file_id is None) == (self.data is None):
            raise ValueError("У изображения — ровно одно: file_id или данные")


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    buttons: tuple[Button, ...] = ()
    # Даты текста — сущностью date_time (0044)
    dates: tuple[DateEntity, ...] = ()
    # Форматирование текста (рассылка, 11.1)
    entities: tuple[TextEntity, ...] = ()
    # С изображением текст становится подписью к нему
    photo: Photo | None = None


class Sender(Protocol):
    """Отправитель сообщений. Ошибки — подклассы `DeliveryError`.

    Возвращает `file_id` отправленного изображения, если оно было: следующие
    сообщения с ним не загружают файл заново."""

    async def send(self, chat_id: int, message: OutgoingMessage) -> str | None: ...


def _keyboard(buttons: tuple[Button, ...]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=button.text,
                url=button.url,
                copy_text=CopyTextButton(text=button.copy_text) if button.copy_text else None,
                callback_data=button.callback_data,
                style=button.style,
            )
        ]
        for button in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _entities(message: OutgoingMessage) -> list[MessageEntity] | None:
    entities = [
        MessageEntity(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            language=entity.language,
        )
        for entity in message.entities
    ]
    entities.extend(
        MessageEntity(
            type="date_time",
            offset=date.offset,
            length=date.length,
            unix_time=date.unix_time,
            date_time_format=DATE_FORMAT,
        )
        for date in message.dates
    )
    return entities or None


# Ошибки прокси (aiohttp-socks): прокси недоступен, не ответил вовремя, отказал
# соединить, ответил не по протоколу или оборвал соединение посреди рукопожатия SOCKS
# (тогда aiohttp-socks пропускает IncompleteReadError — без прокси aiohttp его не бросает)
_PROXY_ERRORS = (
    ProxyConnectionError,
    ProxyError,
    ProxyTimeoutError,
    asyncio.IncompleteReadError,
)


class TelegramSession(AiohttpSession):
    """Сессия Bot API с необязательным прокси (1.28, 0074).

    Ошибки самого прокси (недоступен, отказал соединить, ответил не по протоколу)
    aiogram не оборачивает — здесь они становятся сетевой ошибкой Telegram, как обрыв
    связи без прокси: запрос до Telegram не дошёл.
    """

    def __init__(
        self,
        *,
        timeout: timedelta,
        proxy: str | None = None,
        api: TelegramAPIServer = PRODUCTION,
    ) -> None:
        super().__init__(api=api, timeout=timeout.total_seconds(), proxy=proxy)

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        try:
            return await super().make_request(bot, method, timeout)
        except _PROXY_ERRORS as error:
            raise TelegramNetworkError(
                method=method, message=f"прокси для Telegram: {type(error).__name__}: {error}"
            ) from error


def classify(error: AiogramError) -> DeliveryError:
    """Чем закончилась отправка: по ошибке aiogram — вид недоставки."""
    match error:
        case TelegramForbiddenError() if _NOT_STARTED in str(error):
            return RecipientNotStartedError(str(error))
        case TelegramForbiddenError():
            return RecipientBlockedError(str(error))
        case TelegramRetryAfter() | RestartingTelegram() | TelegramUnauthorizedError():
            return NotAcceptedError(str(error))
        case TelegramEntityTooLarge():
            # В aiogram это подкласс сетевой ошибки, но это ответ 413 — отказ по существу
            return UndeliverableError(str(error))
        case TelegramNetworkError() if isinstance(
            error.__cause__, (ClientConnectorError, *_PROXY_ERRORS)
        ):
            # Соединение не установлено (в том числе прокси не соединил) — запрос до
            # Telegram не дошёл
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
        proxy: str | None = None,
        timeout: timedelta = DEFAULT_TIMEOUT,
    ) -> None:
        api = TelegramAPIServer.from_base(api_base) if api_base else PRODUCTION
        session = TelegramSession(api=api, timeout=timeout, proxy=proxy)
        self._bot = Bot(token, session=session)

    @classmethod
    def for_bot(cls, bot: Bot) -> TelegramSender:
        """Отправитель поверх готового бота — например, бота веб-процесса для тестового
        сообщения рассылки (11.3). Сессию бота закрывает его владелец, не `close()`."""
        sender = cls.__new__(cls)
        sender._bot = bot
        return sender

    async def send(self, chat_id: int, message: OutgoingMessage) -> str | None:
        markup = _keyboard(message.buttons)
        try:
            if message.photo is None:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=message.text,
                    entities=_entities(message),
                    reply_markup=markup,
                )
                return None
            photo = message.photo
            upload = (
                BufferedInputFile(photo.data, filename=photo.filename)
                if photo.data is not None
                else photo.file_id or ""
            )
            sent = await self._bot.send_photo(
                chat_id=chat_id,
                photo=upload,
                caption=message.text or None,
                caption_entities=_entities(message),
                reply_markup=markup,
            )
        except AiogramError as error:
            raise classify(error) from error
        # Самый большой размер — последний (Bot API, PhotoSize)
        return sent.photo[-1].file_id if sent.photo else photo.file_id

    async def close(self) -> None:
        await self._bot.session.close()
