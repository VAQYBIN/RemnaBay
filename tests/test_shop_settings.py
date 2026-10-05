"""Настройки магазина, которые меняются в админке (04-operator-settings.md)."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox, SecretDecryptionError, generate_key
from remnabay.journal import ActorType, JournalEntry
from remnabay.shop_settings import (
    PANEL_OUTAGE_ALERT_AFTER,
    PANEL_SYNC_INTERVAL,
    PANEL_WEBHOOK_SECRET,
    RETRY_MAX_ATTEMPTS,
    RETRY_WINDOW,
    SettingError,
    ShopSetting,
    ShopSettingValue,
    get_setting,
    set_setting,
    webhook_secret,
)
from remnabay.worker.main import retry_policy
from tests.conftest import REQUIRED_ENV
from tests.domain_support import add, make_team_member

BOX = SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])


async def test_defaults_from_operator_settings(db_session: AsyncSession) -> None:
    """4.14, 4.8, 4.13: пока оператор ничего не менял — значения из 04-operator-settings."""
    assert await get_setting(db_session, RETRY_MAX_ATTEMPTS) == 20
    assert await get_setting(db_session, RETRY_WINDOW) == timedelta(hours=1)
    assert await get_setting(db_session, PANEL_SYNC_INTERVAL) == timedelta(minutes=15)
    assert await get_setting(db_session, PANEL_OUTAGE_ALERT_AFTER) == timedelta(minutes=5)


async def test_4_25_change_applies_at_once_and_is_journaled(db_session: AsyncSession) -> None:
    """4.25: изменение действует сразу, без перезапуска; в журнале — кто, что и на что поменял."""
    member = make_team_member()
    await add(db_session, member)

    await set_setting(db_session, RETRY_MAX_ATTEMPTS, 5, member_id=member.id)

    assert await get_setting(db_session, RETRY_MAX_ATTEMPTS) == 5
    entry = (
        await db_session.scalars(
            select(JournalEntry).where(JournalEntry.action == "setting.changed")
        )
    ).one()
    assert entry.actor_type == ActorType.TEAM_MEMBER
    assert entry.actor_ref == str(member.id)
    assert entry.details == {"key": "retry.max_attempts", "old": 20, "new": 5}


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        (RETRY_MAX_ATTEMPTS, 0),
        (RETRY_WINDOW, timedelta(0)),
        (PANEL_SYNC_INTERVAL, timedelta(seconds=-1)),
    ],
)
async def test_invalid_value_is_rejected_and_not_stored(
    db_session: AsyncSession, setting: ShopSetting[Any], value: object
) -> None:
    """Неверное значение не сохраняется: магазин продолжает работать на прежнем."""
    member = make_team_member()
    await add(db_session, member)
    before = await get_setting(db_session, setting)

    with pytest.raises(SettingError):
        await set_setting(db_session, setting, value, member_id=member.id)

    assert await get_setting(db_session, setting) == before


async def test_broken_stored_value_falls_back_to_default(db_session: AsyncSession) -> None:
    """Значение в базе, которое не проходит проверку (например, после правки руками), не
    роняет магазин: действует значение по умолчанию."""
    await add(db_session, ShopSettingValue(key=RETRY_MAX_ATTEMPTS.key, value="много"))

    assert await get_setting(db_session, RETRY_MAX_ATTEMPTS) == 20


async def test_1_9_webhook_secret_is_generated_once_and_stored_encrypted(
    db_session: AsyncSession,
) -> None:
    """1.9: секрет вебхука генерирует магазин; он не меняется от чтения к чтению и лежит
    в базе только зашифрованным (04-operator-settings, «Секреты»)."""
    first = await webhook_secret(db_session, BOX)
    second = await webhook_secret(db_session, BOX)

    assert first == second
    assert len(first) >= 32
    row = await db_session.get(ShopSettingValue, PANEL_WEBHOOK_SECRET.key)
    assert row is not None
    stored = row.value
    assert isinstance(stored, str)
    assert first not in stored
    assert BOX.decrypt(stored) == first


async def test_webhook_secret_with_another_encryption_key_is_not_readable(
    db_session: AsyncSession,
) -> None:
    """Сменили ENCRYPTION_KEY — сохранённый секрет не расшифровывается, а не подменяется молча."""
    await webhook_secret(db_session, BOX)

    with pytest.raises(SecretDecryptionError):
        await webhook_secret(db_session, SecretBox(generate_key()))


async def test_4_14_worker_reads_retry_limits_from_settings(db_session: AsyncSession) -> None:
    """4.14: лимит попыток и окно повторов воркер берёт из настроек при каждом решении."""
    member = make_team_member()
    await add(db_session, member)
    await set_setting(db_session, RETRY_MAX_ATTEMPTS, 7, member_id=member.id)
    await set_setting(db_session, RETRY_WINDOW, timedelta(minutes=30), member_id=member.id)

    policy = await retry_policy(db_session)

    assert policy.max_attempts == 7
    assert policy.max_age == timedelta(minutes=30)
