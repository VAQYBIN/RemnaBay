"""Клиенты магазина: учёт по Telegram-аккаунту, когда человек пишет боту."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client, TelegramAccount


@dataclass(frozen=True)
class TelegramUser:
    """Аккаунт Telegram, от которого пришло обновление."""

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


async def register_telegram_user(
    session: AsyncSession, user: TelegramUser, *, now: datetime
) -> Client:
    """Клиент по Telegram-аккаунту: найденный или новый.

    Один Telegram-аккаунт — один клиент (01-domain, «Способ входа»). Новый клиент
    впервые открыл бот сейчас (1.17); усыновлённый получает эту дату при первом
    открытии. Открыв бот, клиент снимает пометку «бот заблокирован» (11.9).
    Имя, username и язык берутся из Telegram при каждом обновлении.
    """
    account = await session.get(TelegramAccount, user.id)
    if account is None:
        account = await _create(session, user, now)
    client = await session.get_one(Client, account.client_id)

    account.username = user.username
    account.first_name = user.first_name
    account.last_name = user.last_name
    if user.language_code:
        client.language_code = user.language_code
    if client.bot_started_at is None:
        client.bot_started_at = now
    if client.first_seen_at is None:
        client.first_seen_at = now
    client.bot_blocked_at = None
    await session.flush()
    return client


async def _create(session: AsyncSession, user: TelegramUser, now: datetime) -> TelegramAccount:
    """Новый клиент со способом входа. Если два обновления одного человека пришли
    одновременно, второе находит клиента, созданного первым."""
    try:
        async with session.begin_nested():
            client = Client(first_seen_at=now, bot_started_at=now)
            session.add(client)
            await session.flush()
            account = TelegramAccount(telegram_id=user.id, client_id=client.id)
            session.add(account)
            await session.flush()
            return account
    except IntegrityError:
        account = await session.scalar(
            select(TelegramAccount).where(TelegramAccount.telegram_id == user.id)
        )
        if account is None:
            raise
        return account
