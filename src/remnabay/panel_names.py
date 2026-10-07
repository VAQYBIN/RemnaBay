"""Имена пользователей, которых магазин создаёт в панели (решение 0057).

Имя — `<префикс>_<Telegram ID>_<N>`, где N — порядковый номер подписки клиента,
созданной магазином. Префикс задаёт оператор в «Настройки → Панель». Он
фиксируется после сохранения или когда магазин создал первого пользователя в
панели: так имена в панели никогда не смешиваются.
"""

from typing import Annotated

from pydantic import Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.shop_settings import JOURNAL_SUBJECT, ShopSetting, ShopSettingValue

# Латиница, цифры и дефис; до 16 символов — тогда имя с Telegram ID и номером
# укладывается в ограничение панели (3–36 символов из букв, цифр, «_» и «-»)
PREFIX_PATTERN = r"^[A-Za-z0-9-]{1,16}$"
DEFAULT_PREFIX = "rb"
# В режиме разработки — свой префикс: проверки на рабочей панели не смешиваются с клиентами
DEV_PREFIX = "rbtest"

PANEL_USERNAME_PREFIX = ShopSetting(
    "panel.username_prefix",
    TypeAdapter[str](Annotated[str, Field(pattern=PREFIX_PATTERN)]),
    DEFAULT_PREFIX,
)


class PrefixLockedError(Exception):
    """Префикс уже зафиксирован — изменить его нельзя."""


async def _stored_prefix(session: AsyncSession) -> str | None:
    value = await session.scalar(
        select(ShopSettingValue.value).where(ShopSettingValue.key == PANEL_USERNAME_PREFIX.key)
    )
    return value if isinstance(value, str) else None


async def username_prefix(session: AsyncSession, *, dev_mode: bool) -> tuple[str, bool]:
    """Действующий префикс и зафиксирован ли он."""
    stored = await _stored_prefix(session)
    if stored is not None:
        return stored, True
    return (DEV_PREFIX if dev_mode else DEFAULT_PREFIX), False


async def save_prefix(session: AsyncSession, value: str, *, member_id: int) -> str:
    """Оператор сохраняет префикс — один раз (решение 0057). `SettingError` — неверный
    формат, `PrefixLockedError` — уже зафиксирован."""
    prefix = PANEL_USERNAME_PREFIX.parse(value)
    saved = await session.scalar(
        insert(ShopSettingValue)
        .values(key=PANEL_USERNAME_PREFIX.key, value=prefix, updated_by_id=member_id)
        .on_conflict_do_nothing()
        .returning(ShopSettingValue.key)
    )
    if saved is None:
        raise PrefixLockedError("Префикс уже зафиксирован")
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="setting.changed",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, PANEL_USERNAME_PREFIX.key),
        details={"key": PANEL_USERNAME_PREFIX.key, "old": None, "new": prefix},
    )
    return prefix


async def lock_prefix(session: AsyncSession, *, dev_mode: bool) -> str:
    """Префикс для нового пользователя панели; действующий по умолчанию фиксируется."""
    prefix, locked = await username_prefix(session, dev_mode=dev_mode)
    if locked:
        return prefix
    await session.execute(
        insert(ShopSettingValue)
        .values(key=PANEL_USERNAME_PREFIX.key, value=prefix)
        .on_conflict_do_nothing()
    )
    stored = await _stored_prefix(session)
    if stored != prefix:
        # Оператор успел сохранить свой префикс одновременно — действует его
        return stored or prefix
    await record(
        session,
        actor=Actor.SYSTEM,
        action="setting.locked",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, PANEL_USERNAME_PREFIX.key),
        details={"key": PANEL_USERNAME_PREFIX.key, "value": prefix},
    )
    return prefix


def panel_username(prefix: str, telegram_id: int, number: int) -> str:
    return f"{prefix}_{telegram_id}_{number}"
