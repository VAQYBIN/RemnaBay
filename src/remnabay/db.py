"""Подключение к PostgreSQL магазина: async SQLAlchemy 2 и драйвер psycopg 3 (решение 0023)."""

from enum import StrEnum

from sqlalchemy import Enum, MetaData
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
