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
    GetMyCommands,
    GetMyDescription,
    GetMyName,
    GetMyShortDescription,
    SendMessage,
    SendRichMessage,
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyProfilePhoto,
    SetMyShortDescription,
    SetWebhook,
    TelegramMethod,
)
from aiogram.types import (
    BotCommand,
    BotDescription,
    BotName,
    BotShortDescription,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockUnion,
    Message,
    RichMessageButton,
    Update,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot import SessionFactory, create_bot, create_dispatcher
from remnabay.crypto import SecretBox
from remnabay.payments import Providers
from tests.conftest import REQUIRED_ENV
from tests.panel_support import FakePanel

BOT_TOKEN = REQUIRED_ENV["BOT_TOKEN"]
BOT_USERNAME = "shop_test_bot"
BOT_NAME = "Тестовый магазин"
ADMIN_LOGIN_URL = "https://shop.example.com/admin/login"
_ids = count(1)


class FakeTelegram(BaseSession):
    """Bot API без сети: запросы записываются, ответы — правдоподобные."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # Ошибка Telegram на следующий запрос этого вида (например, лимит на смену имени, 1.27)
        self.failures: dict[type[TelegramMethod[Any]], Exception] = {}
        # Профиль бота в Telegram: что задали «Применить в Telegram» (1.27)
        self.name = ""
        self.description = ""
        self.short_description = ""
        self.commands: list[BotCommand] = []

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
        failure = self.failures.pop(type(method), None)
        if failure is not None:
            raise failure
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
            case SendRichMessage():
                return Message(
                    message_id=next(_ids),
                    date=datetime.now(UTC),
                    chat=Chat(id=cast(int, method.chat_id), type="private"),
                )
            case AnswerCallbackQuery() | SetWebhook() | DeleteWebhook():
                return True
            case GetMyName():
                return BotName(name=self.name)
            case GetMyDescription():
                return BotDescription(description=self.description)
            case GetMyShortDescription():
                return BotShortDescription(short_description=self.short_description)
            case GetMyCommands():
                return self.commands
            case SetMyName():
                self.name = method.name or ""
                return True
            case SetMyDescription():
                self.description = method.description or ""
                return True
            case SetMyShortDescription():
                self.short_description = method.short_description or ""
                return True
            case SetMyCommands():
                self.commands = list(method.commands)
                return True
            case SetMyProfilePhoto():
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

    def screens(self) -> list[SendMessage | SendRichMessage | EditMessageText]:
        """Всё, что бот показал клиенту: новые сообщения и правки живого меню."""
        return [
            r
            for r in self.requests
            if isinstance(r, SendMessage | SendRichMessage | EditMessageText)
        ]

    def last(self) -> SendMessage | SendRichMessage | EditMessageText:
        return self.screens()[-1]


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
    dispatcher = create_dispatcher(
        same_session(session),
        admin_login_url=ADMIN_LOGIN_URL,
        providers=Providers(SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"]), []),
        panel=FakePanel().client(),
        shop_url=REQUIRED_ENV["PUBLIC_URL"],
    )
    yield BotHarness(bot=bot, dispatcher=dispatcher, telegram=telegram)


@dataclass(frozen=True)
class AnyButton:
    """Кнопка экрана — внутри rich-сообщения или под сообщением, в одном виде."""

    text: str
    style: str | None = None
    url: str | None = None
    callback_data: str | None = None
    copy_text: str | None = None


def _view(button: InlineKeyboardButton | RichMessageButton) -> AnyButton:
    text = button.text
    assert isinstance(text, str)
    return AnyButton(
        text=text,
        style=button.style,
        url=button.url,
        callback_data=button.callback_data,
        copy_text=button.copy_text.text if button.copy_text is not None else None,
    )


def _rich_buttons(blocks: list[InputRichBlockUnion]) -> list[RichMessageButton]:
    found: list[RichMessageButton] = []
    for block in blocks:
        if isinstance(block, InputRichBlockButtons):
            found.extend(block.buttons)
        elif isinstance(block, InputRichBlockDetails):
            found.extend(_rich_buttons(block.blocks))
    return found


def screen_buttons(screen: SendMessage | SendRichMessage | EditMessageText) -> list[AnyButton]:
    """Кнопки экрана: сначала внутри rich-сообщения (0044), затем под сообщением."""
    buttons: list[InlineKeyboardButton | RichMessageButton] = []
    rich = None if isinstance(screen, SendMessage) else screen.rich_message
    if rich is not None:
        buttons.extend(_rich_buttons(rich.blocks or []))
    markup = screen.reply_markup
    if isinstance(markup, InlineKeyboardMarkup):
        buttons.extend(button for row in markup.inline_keyboard for button in row)
    return [_view(button) for button in buttons]
