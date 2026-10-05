"""Бот в тестах: поддельный Bot API и обновления от имени пользователя Telegram."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteWebhook,
    EditMessageText,
    GetMe,
    SendMessage,
    SetWebhook,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot import SessionFactory, create_bot, create_dispatcher
from tests.conftest import REQUIRED_ENV

BOT_TOKEN = REQUIRED_ENV["BOT_TOKEN"]
BOT_USERNAME = "shop_test_bot"
BOT_NAME = "Тестовый магазин"
ADMIN_LOGIN_URL = "https://shop.example.com/api/admin/auth/telegram"
_ids = count(1)


class FakeTelegram(BaseSession):
    """Bot API без сети: запросы записываются, ответы — правдоподобные."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура aiogram
    ) -> Any:
        del timeout
        self.requests.append(method)
        match method:
            case GetMe():
                return User(id=bot.id, is_bot=True, first_name=BOT_NAME, username=BOT_USERNAME)
            case SendMessage() | EditMessageText():
                chat_id = cast(int, method.chat_id)
                return Message(
                    message_id=next(_ids),
                    date=datetime.now(UTC),
                    chat=Chat(id=chat_id, type="private"),
                    text=method.text,
                )
            case AnswerCallbackQuery() | SetWebhook() | DeleteWebhook():
                return True
            case _:
                raise AssertionError(f"Запрос не поддержан в тестах: {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        del url, headers, timeout, chunk_size, raise_for_status
        yield b""

    def sent(self) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage)]

    def texts(self) -> list[str]:
        return [r.text for r in self.sent()]


@dataclass
class BotHarness:
    """Бот и диспетчер магазина поверх транзакции теста."""

    bot: Bot
    dispatcher: Dispatcher
    telegram: FakeTelegram
    updates: count[int] = field(default_factory=lambda: count(1))

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def send_text(self, user: User, text: str) -> None:
        message = Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text=text,
        )
        await self.feed(Update(update_id=next(self.updates), message=message))

    async def press(self, user: User, data: str, message_id: int = 1) -> None:
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            text="…",
        )
        query = CallbackQuery(
            id=str(next(_ids)), from_user=user, chat_instance="ci", message=message, data=data
        )
        await self.feed(Update(update_id=next(self.updates), callback_query=query))


def telegram_user(
    user_id: int = 100, *, username: str | None = "client", language_code: str | None = "ru"
) -> User:
    return User(
        id=user_id,
        is_bot=False,
        first_name="Иван",
        username=username,
        language_code=language_code,
    )


def same_session(session: AsyncSession) -> SessionFactory:
    """Фабрика сессий, которая всегда отдаёт сессию теста: бот работает в её транзакции."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[AsyncSession]:
        yield session

    return factory


@asynccontextmanager
async def bot_harness(session: AsyncSession) -> AsyncGenerator[BotHarness]:
    """Диспетчер, который работает в той же сессии, что и проверки теста."""
    telegram = FakeTelegram()
    bot = create_bot(BOT_TOKEN, session=telegram)
    dispatcher = create_dispatcher(same_session(session), admin_login_url=ADMIN_LOGIN_URL)
    yield BotHarness(bot=bot, dispatcher=dispatcher, telegram=telegram)
