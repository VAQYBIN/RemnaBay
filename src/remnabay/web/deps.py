"""Зависимости обработчиков веба: сессия базы и шифрование секретов."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remnabay.crypto import SecretBox


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос. Фиксирует обработчик сам (`commit()`), до ответа: код после
    `yield` FastAPI выполняет уже после отправки ответа."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


def get_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.box
    return box
