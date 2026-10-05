"""Контекст обновления бота: сессия базы, клиент, участник команды, тексты."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram.types import Chat, TelegramObject, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.clients import TelegramUser, register_telegram_user
from remnabay.domain.clients import Client
from remnabay.domain.team import TeamMember
from remnabay.messaging import load_texts
from remnabay.texts import Texts

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# Ключ контекста в данных обработчика aiogram: обработчик объявляет `ctx: BotContext`
CONTEXT_KEY = "ctx"
SESSIONS_KEY = "sessions"


@dataclass(frozen=True)
class BotContext:
    session: AsyncSession
    client: Client
    # Действующий участник команды, если человек в ней состоит
    member: TeamMember | None
    texts: Texts

    def render(self, key: str, /, **variables: str) -> str:
        """Текст по ключу на языке клиента (0018)."""
        return self.texts.render(key, self.client.language_code, **variables)


async def active_member(session: AsyncSession, telegram_id: int) -> TeamMember | None:
    return await session.scalar(
        select(TeamMember).where(
            TeamMember.telegram_id == telegram_id, TeamMember.revoked_at.is_(None)
        )
    )


async def context_middleware(handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
    """Каждое обновление из личного чата — в своей транзакции; клиент учитывается сразу.

    Обновления не из личного чата (группы, каналы) бот не обрабатывает.
    """
    user: User | None = data.get("event_from_user")
    chat: Chat | None = data.get("event_chat")
    if user is None or user.is_bot or (chat is not None and chat.type != "private"):
        return None

    sessions: SessionFactory = data[SESSIONS_KEY]
    async with sessions() as session:
        client = await register_telegram_user(
            session,
            TelegramUser(
                id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code,
            ),
            now=datetime.now(UTC),
        )
        data[CONTEXT_KEY] = BotContext(
            session=session,
            client=client,
            member=await active_member(session, user.id),
            texts=await load_texts(session),
        )
        result = await handler(event, data)
        await session.commit()
        return result
