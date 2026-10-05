"""Веб магазина в тестах: API админки, бот с поддельным Bot API, транзакция теста."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count

import httpx2
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access import create_session
from remnabay.config import load_settings
from remnabay.domain.team import LoginMethod, TeamMember
from remnabay.web.admin import SESSION_COOKIE
from remnabay.web.app import create_app
from remnabay.web.deps import get_session
from tests.bot_support import FakeTelegram, same_session

# Запросы админки идут со страницы магазина (проверка Origin, 0051)
SHOP_ORIGIN = "https://shop.example.com"
_ids = count(1000)


@dataclass
class Shop:
    """Веб и бот магазина поверх одной транзакции теста."""

    app: FastAPI
    http: httpx2.AsyncClient
    telegram: FakeTelegram
    session: AsyncSession
    updates: count[int] = field(default_factory=lambda: count(1))

    @property
    def bot(self) -> Bot:
        bot: Bot = self.app.state.bot
        return bot

    @property
    def dispatcher(self) -> Dispatcher:
        dispatcher: Dispatcher = self.app.state.dispatcher
        return dispatcher

    async def send_text(self, user: User, text: str) -> None:
        message = Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text=text,
        )
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=next(self.updates), message=message)
        )

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
        update = Update(update_id=next(self.updates), callback_query=query)
        await self.dispatcher.feed_update(self.bot, update)

    async def sign_in(self, member: TeamMember) -> None:
        """Сессия участника без прохождения входа — для тестов других разделов."""
        token, _ = await create_session(
            self.session, member, LoginMethod.BOT_CONFIRM, now=datetime.now(UTC)
        )
        self.http.cookies.set(SESSION_COOKIE, token)


@asynccontextmanager
async def running_shop(session: AsyncSession) -> AsyncGenerator[Shop]:
    """Магазин с настройками из окружения теста (нужна фикстура `valid_env`)."""
    app = create_app(load_settings(env_file=None))
    telegram = FakeTelegram()
    app.state.bot.session = telegram
    app.state.dispatcher.workflow_data["sessions"] = same_session(session)

    async def test_session() -> AsyncGenerator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = test_session
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url=SHOP_ORIGIN, headers={"Origin": SHOP_ORIGIN}
    ) as http:
        yield Shop(app=app, http=http, telegram=telegram, session=session)
