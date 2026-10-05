"""Окружение Alembic: миграции схемы через async-движок магазина."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from remnabay.config import load_settings
from remnabay.schema import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # `remnabay migrate` передаёт адрес сам; при запуске `alembic` из консоли
    # разработчика адрес берётся из .env с той же проверкой (1.1)
    url = config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return load_settings().sqlalchemy_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
