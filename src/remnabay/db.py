"""Подключение к PostgreSQL магазина: async SQLAlchemy 2 и драйвер psycopg 3 (решение 0023)."""

from enum import StrEnum

import psycopg
from sqlalchemy import Enum, MetaData
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from remnabay.config import Settings

# Явные имена ограничений: Alembic генерирует стабильные миграции,
# а ограничения можно удалять и переименовывать по имени
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей. Все модули с моделями перечислены в `remnabay.schema`."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum_values(members: type[StrEnum]) -> list[str]:
    return [member.value for member in members]


def str_enum_type[E: StrEnum](enum_class: type[E], name: str) -> Enum:
    """Колонка-перечисление: строка с CHECK, а не тип ENUM в PostgreSQL.

    В базе хранятся значения (`"team_member"`), а не имена членов; новое значение
    добавляется миграцией без ALTER TYPE.
    """
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=_enum_values,
    )


def create_engine(settings: Settings, *, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_database_url, pool_pre_ping=True, pool_size=pool_size
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Operator Intervention: база останавливается (admin, crash) или ещё запускается
_SHUTDOWN_STATES = frozenset({"57P01", "57P02", "57P03"})


def database_unavailable(error: BaseException) -> bool:
    """Ошибка значит, что база не отвечает (нет соединения), а не что запрос неверен.

    У psycopg отказ соединения — `OperationalError` без кода SQLSTATE. С кодом база
    сообщает о том же, когда соединение потеряно (класс 08) или она останавливается
    либо ещё запускается (57P01–57P03). Остальные коды (например, взаимная
    блокировка) приходят от работающей базы.
    """
    if isinstance(error, OSError | TimeoutError):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    original = error.orig
    if not isinstance(original, psycopg.OperationalError):
        return False
    state = original.sqlstate
    return state is None or state.startswith("08") or state in _SHUTDOWN_STATES


def describe_error(error: BaseException) -> str:
    """Ошибка одной строкой для лога: тип и первая строка сообщения, без трассировки.

    У ошибок SQLAlchemy берётся исходная ошибка драйвера: сообщение самой SQLAlchemy
    многострочное (запрос, ссылка на описание).
    """
    if isinstance(error, DBAPIError) and error.orig is not None:
        error = error.orig
    lines = str(error).strip().splitlines()
    return f"{type(error).__name__}: {lines[0]}" if lines else type(error).__name__
