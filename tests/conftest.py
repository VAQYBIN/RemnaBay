"""Общие фикстуры тестов."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from remnabay.config import load_settings
from remnabay.migrations import upgrade_to_head
from tests import queue_support

REQUIRED_ENV = {
    "BOT_TOKEN": "123456:test-bot-token",
    "PANEL_URL": "https://panel.example.com",
    "PANEL_TOKEN": "test-panel-token",
    "DATABASE_URL": os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://remnabay:remnabay@localhost:5432/remnabay_test",
    ),
    "OWNER_TELEGRAM_ID": "100500",
    "PUBLIC_URL": "https://shop.example.com",
    # Ключ только для тестов, в формате remnabay generate-key
    "ENCRYPTION_KEY": "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktdGVzdC0=",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Окружение без параметров магазина и без чужого `.env` в рабочей папке."""
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def valid_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Полный набор обязательных параметров."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(REQUIRED_ENV)


@pytest.fixture(scope="session")
def database_url() -> str:
    """Адрес тестовой базы, к которой применены все миграции (один раз на прогон)."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in REQUIRED_ENV.items():
            monkeypatch.setenv(name, value)
        settings = load_settings(env_file=None)
    upgrade_to_head(settings)
    return settings.sqlalchemy_database_url


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Сессия внутри внешней транзакции, которая откатывается после теста.

    `commit()` в коде под тестом фиксирует только точку сохранения: тесты не
    оставляют данных и не мешают друг другу.
    """
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def queue_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Движок для тестов очереди: данные фиксируются по-настоящему, как у воркера.

    Таблицы очереди очищаются до и после теста. Номера задач не сбрасываются:
    записи журнала о прошлых задачах остаются (4.26) и не должны совпасть с новыми.
    """
    engine = create_async_engine(database_url, pool_size=10)
    queue_support.PANEL["up"] = True
    queue_support.FAILING.clear()
    await queue_support.prepare(engine)
    try:
        yield engine
    finally:
        await queue_support.prepare(engine)
        await engine.dispose()
