"""Зависимости обработчиков веба: сессия базы, шифрование секретов, платёжные провайдеры."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remnabay.crypto import SecretBox
from remnabay.payments import Providers


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос. Фиксирует обработчик сам (`commit()`), до ответа: код после
    `yield` FastAPI выполняет уже после отправки ответа."""
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


def get_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.box
    return box


def get_providers(request: Request) -> Providers:
    providers: Providers = request.app.state.providers
    return providers
