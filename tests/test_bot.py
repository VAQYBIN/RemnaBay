"""Приём обновлений бота: учёт клиента, ответ на непонятное, вебхук Telegram (0051)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot import TELEGRAM_WEBHOOK_PATH
from remnabay.crypto import SecretBox
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.shop import SHOP_STATE, ShopState
from remnabay.shop_settings import TELEGRAM_WEBHOOK_SECRET, generated_secret
from remnabay.web.telegram_webhook import SECRET_HEADER
from tests.bot_support import bot_harness, telegram_user
from tests.conftest import REQUIRED_ENV
from tests.domain_support import add, put_setting
from tests.web_support import Shop, running_shop

BOX = SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])
UNKNOWN = "Не совсем понял. Откройте главное меню — там всё самое нужное."


async def _client_of(session: AsyncSession, telegram_id: int) -> Client:
    account = await session.get_one(TelegramAccount, telegram_id)
    return await session.get_one(Client, account.client_id)


async def test_first_message_registers_client(db_session: AsyncSession) -> None:
    """Первое сообщение — новый клиент: впервые открыл бот сейчас (данные для 1.17)."""
    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100, username="ivan"), "привет")

    account = await db_session.get_one(TelegramAccount, 100)
    client = await db_session.get_one(Client, account.client_id)
    assert account.username == "ivan"
    assert client.bot_started_at is not None
    assert client.first_seen_at == client.bot_started_at
    assert client.language_code == "ru"


async def test_repeated_messages_keep_one_client(db_session: AsyncSession) -> None:
    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "раз")
        first = await _client_of(db_session, 100)
        started = first.bot_started_at
        await harness.send_text(telegram_user(100, username="renamed"), "два")

    accounts = list(
        await db_session.scalars(select(TelegramAccount).where(TelegramAccount.telegram_id == 100))
    )
    assert len(accounts) == 1
    assert accounts[0].username == "renamed"
    assert (await _client_of(db_session, 100)).bot_started_at == started


async def test_adopted_client_gets_bot_start_on_first_open(db_session: AsyncSession) -> None:
    """Усыновлённый клиент впервые открыл бот — дата появляется, пометка остаётся."""
    adopted_at = datetime(2026, 9, 1, tzinfo=UTC)
    client = Client(first_seen_at=adopted_at, adopted_at=adopted_at)
    await add(db_session, client)
    await add(db_session, TelegramAccount(telegram_id=200, client_id=client.id))

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(200), "привет")

    await db_session.refresh(client)
    assert client.bot_started_at is not None
    assert client.first_seen_at == adopted_at
    assert client.adopted_at == adopted_at


async def test_11_9_opening_bot_clears_blocked_mark(db_session: AsyncSession) -> None:
    """11.9: клиент снова открыл бот — пометка «бот заблокирован» снимается."""
    client = Client(bot_blocked_at=datetime(2026, 9, 1, tzinfo=UTC))
    await add(db_session, client)
    await add(db_session, TelegramAccount(telegram_id=300, client_id=client.id))

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(300), "я вернулся")

    await db_session.refresh(client)
    assert client.bot_blocked_at is None


async def test_unknown_message_gets_fallback(db_session: AsyncSession) -> None:
    """Непонятное сообщение — `fallback.unknown` (03-bot-texts.md)."""
    await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "что-то непонятное")

    assert harness.telegram.texts() == [UNKNOWN]


async def test_group_messages_are_ignored(db_session: AsyncSession) -> None:
    """Бот работает только в личном чате: из группы — ни ответа, ни клиента."""
    async with bot_harness(db_session) as harness:
        user = telegram_user(400)
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=-1001, type="supergroup"),
            from_user=user,
            text="привет",
        )
        await harness.feed(Update(update_id=1, message=message))

    assert harness.telegram.requests == []
    assert await db_session.get(TelegramAccount, 400) is None


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncIterator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


def _update(text: str = "привет") -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1_790_000_000,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 100, "is_bot": False, "first_name": "Иван"},
            "text": text,
        },
    }


async def test_0051_webhook_with_secret_is_handled(shop: Shop, db_session: AsyncSession) -> None:
    """0051: обновление с верным секретом обрабатывается."""
    client, telegram = shop.http, shop.telegram
    await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
    secret = await generated_secret(db_session, BOX, TELEGRAM_WEBHOOK_SECRET)

    response = await client.post(
        TELEGRAM_WEBHOOK_PATH, json=_update(), headers={SECRET_HEADER: secret}
    )

    assert response.status_code == 200
    assert telegram.texts() == [UNKNOWN]


@pytest.mark.parametrize("header", [None, "wrong"])
async def test_0051_webhook_without_secret_is_rejected(shop: Shop, header: str | None) -> None:
    """0051: без верного секрета обновление отклоняется и не обрабатывается."""
    client, telegram = shop.http, shop.telegram
    headers = {SECRET_HEADER: header} if header else {}

    response = await client.post(TELEGRAM_WEBHOOK_PATH, json=_update(), headers=headers)

    assert response.status_code == 401
    assert telegram.requests == []
