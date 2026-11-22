"""«Настройки → Панель»: секрет вебхука — свой или созданный магазином (1.9, решение 0052);
интервал сверки (4.8) и уведомление о недоступности панели (4.13)."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.messaging import SendArgs
from remnabay.panel import SIGNATURE_HEADER
from remnabay.panel._webhooks import sign
from remnabay.panel_sync import SYNC_ALL, current_outage, health_check
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask
from remnabay.shop_settings import (
    PANEL_OUTAGE_ALERT_AFTER,
    PANEL_SYNC_INTERVAL,
    SettingError,
    check_webhook_secret,
    get_setting,
    new_webhook_secret,
    webhook_secret,
)
from remnabay.web.panel_webhook import PANEL_WEBHOOK_PATH
from tests.conftest import REQUIRED_ENV, journaled_in_test
from tests.domain_support import add, make_team_member
from tests.panel_support import FakePanel, fake_runtime, user_json
from tests.web_support import Shop, running_shop

BOX = SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])
SETTINGS = "/api/admin/settings/panel"
INTERVALS = f"{SETTINGS}/intervals"
OWN_SECRET = "Ab12" * 10  # 40 символов, только буквы и цифры


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = make_team_member(telegram_id=500, role=TeamRole.OWNER)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


def test_0052_generated_secret_fits_panel_rules() -> None:
    """Панель принимает только буквы и цифры, не меньше 32 символов (WEBHOOK_SECRET_HEADER)."""
    for _ in range(50):
        secret = new_webhook_secret()
        assert check_webhook_secret(secret) == secret
        assert secret.isalnum()
        assert len(secret) >= 32


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("A" * 31, "не короче 32"),
        ("Ab-12" * 8, "только латинские буквы и цифры"),
        ("Секрет" * 8, "только латинские буквы и цифры"),
    ],
)
def test_0052_secret_rules(value: str, message: str) -> None:
    with pytest.raises(SettingError, match=message):
        check_webhook_secret(value)


async def test_1_9_panel_settings_show_address_and_secret(shop: Shop, owner: TeamMember) -> None:
    del owner
    body = (await shop.http.get(SETTINGS)).json()

    assert body["webhook_url"] == "https://shop.example.com/webhooks/panel"
    assert body["webhook_secret"] == await webhook_secret(shop.session, BOX)
    assert body["webhook_secret_fits_panel"] is True


async def test_0052_own_secret_is_used_for_signature(shop: Shop, owner: TeamMember) -> None:
    """0052: свой секрет (уже заданный в панели) — вебхуки с его подписью принимаются."""
    response = await shop.http.put(
        f"{SETTINGS}/webhook-secret", json={"webhook_secret": OWN_SECRET}
    )

    assert response.json()["webhook_secret"] == OWN_SECRET
    body = json.dumps(
        {
            "scope": "user",
            "event": "user.modified",
            "timestamp": "2026-10-06T12:00:00.000Z",
            "data": user_json(id=7),
        }
    ).encode()
    accepted = await shop.http.post(
        PANEL_WEBHOOK_PATH,
        content=body,
        headers={SIGNATURE_HEADER: sign(body, OWN_SECRET), "Content-Type": "application/json"},
    )
    assert accepted.status_code == 200
    entries = await shop.session.scalars(
        select(JournalEntry).where(
            journaled_in_test(),
            JournalEntry.actor_ref == str(owner.id),
            JournalEntry.action == "setting.changed",
        )
    )
    details = [entry.details for entry in entries]
    assert details == [{"key": "panel.webhook_secret"}]
    assert OWN_SECRET not in json.dumps(details)


async def test_0052_invalid_own_secret_is_rejected(shop: Shop, owner: TeamMember) -> None:
    del owner
    before = (await shop.http.get(SETTINGS)).json()["webhook_secret"]

    response = await shop.http.put(f"{SETTINGS}/webhook-secret", json={"webhook_secret": "short"})

    assert response.status_code == 422
    assert (await shop.http.get(SETTINGS)).json()["webhook_secret"] == before


async def test_0052_new_secret_can_be_generated(shop: Shop, owner: TeamMember) -> None:
    del owner
    before = (await shop.http.get(SETTINGS)).json()["webhook_secret"]

    after = (await shop.http.post(f"{SETTINGS}/webhook-secret/generate")).json()["webhook_secret"]

    assert after != before
    assert check_webhook_secret(after) == after


async def test_0052_panel_settings_owner_only(shop: Shop) -> None:
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)

    assert (await shop.http.get(SETTINGS)).status_code == 403
    put = await shop.http.put(f"{SETTINGS}/webhook-secret", json={"webhook_secret": OWN_SECRET})
    assert put.status_code == 403


# --- Интервал сверки и уведомление о недоступности (4.8, 4.13) ---


async def _changes(shop: Shop, owner: TeamMember) -> list[Any]:
    entries = await shop.session.scalars(
        select(JournalEntry)
        .where(
            journaled_in_test(),
            JournalEntry.actor_ref == str(owner.id),
            JournalEntry.action == "setting.changed",
        )
        .order_by(JournalEntry.id)
    )
    return [entry.details for entry in entries]


async def test_4_8_4_13_panel_intervals_shown_with_defaults(shop: Shop, owner: TeamMember) -> None:
    """4.8, 4.13: на странице «Панель» — интервал сверки (15 минут) и через сколько
    уведомлять о недоступности (5 минут)."""
    del owner
    body = (await shop.http.get(SETTINGS)).json()

    assert body["sync_interval_minutes"] == 15
    assert body["outage_alert_minutes"] == 5


async def test_4_8_4_13_4_25_panel_intervals_saved_and_journaled(
    shop: Shop, owner: TeamMember
) -> None:
    """4.8, 4.13, 4.25: владелец меняет интервал сверки и время до уведомления; каждое
    изменение — в журнале с участником, прежним и новым значением. Неизменённое значение
    в журнал не попадает."""
    response = await shop.http.put(
        INTERVALS, json={"sync_interval_minutes": 30, "outage_alert_minutes": 5}
    )

    assert response.status_code == 200
    assert response.json()["sync_interval_minutes"] == 30
    assert await get_setting(shop.session, PANEL_SYNC_INTERVAL) == timedelta(minutes=30)
    assert await _changes(shop, owner) == [
        {"key": "panel.sync_interval", "old": "PT15M", "new": "PT30M"}
    ]

    await shop.http.put(INTERVALS, json={"sync_interval_minutes": 30, "outage_alert_minutes": 10})
    assert await get_setting(shop.session, PANEL_OUTAGE_ALERT_AFTER) == timedelta(minutes=10)
    assert (await _changes(shop, owner))[1:] == [
        {"key": "panel.outage_alert_after", "old": "PT5M", "new": "PT10M"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"sync_interval_minutes": 4, "outage_alert_minutes": 5},
        {"sync_interval_minutes": 1441, "outage_alert_minutes": 5},
        {"sync_interval_minutes": 15, "outage_alert_minutes": 0},
        {"sync_interval_minutes": 15, "outage_alert_minutes": 1441},
    ],
)
async def test_4_8_4_13_panel_intervals_out_of_limits_rejected(
    shop: Shop, owner: TeamMember, body: dict[str, int]
) -> None:
    """4.8, 4.13: значение вне пределов (сверка 5–1440 минут, уведомление 1–1440 минут)
    не сохраняется и не попадает в журнал."""
    response = await shop.http.put(INTERVALS, json=body)

    assert response.status_code == 422
    assert await get_setting(shop.session, PANEL_SYNC_INTERVAL) == timedelta(minutes=15)
    assert await get_setting(shop.session, PANEL_OUTAGE_ALERT_AFTER) == timedelta(minutes=5)
    assert await _changes(shop, owner) == []


async def test_4_8_new_sync_interval_applies_without_restart(shop: Shop, owner: TeamMember) -> None:
    """4.8: воркер планирует сверку с интервалом, который сохранён сейчас, — без перезапуска."""
    del owner
    assert await SYNC_ALL.interval(shop.session) == timedelta(minutes=15)

    await shop.http.put(INTERVALS, json={"sync_interval_minutes": 60, "outage_alert_minutes": 5})

    assert await SYNC_ALL.interval(shop.session) == timedelta(minutes=60)


async def test_4_13_new_alert_delay_applies_without_restart(shop: Shop, owner: TeamMember) -> None:
    """4.13: простой длится 2 минуты — по умолчанию (5 минут) уведомления нет; владелец
    поставил 1 минуту — следующая проверка связи уведомляет команду."""
    del owner
    panel = FakePanel(down=True)
    context = TaskContext(session=shop.session, task_id=1, attempt_number=1)

    async def check() -> list[str]:
        with fake_runtime(panel=panel):
            await health_check.run(context, {})
        await shop.session.flush()
        rows = await shop.session.scalars(
            select(QueueTask.args).where(QueueTask.name == "messages.send")
        )
        return [SendArgs.model_validate(args).text_key for args in rows]

    await check()
    outage = await current_outage(shop.session)
    assert outage is not None
    outage.started_at = datetime.now(UTC) - timedelta(minutes=2)
    assert await check() == []

    await shop.http.put(INTERVALS, json={"sync_interval_minutes": 15, "outage_alert_minutes": 1})

    assert await check() == ["team.panel_unavailable"]


async def test_panel_intervals_owner_only(shop: Shop) -> None:
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)

    put = await shop.http.put(
        INTERVALS, json={"sync_interval_minutes": 30, "outage_alert_minutes": 5}
    )
    assert put.status_code == 403
