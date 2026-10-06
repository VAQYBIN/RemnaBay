"""Секрет вебхука панели: свой или созданный магазином (1.9, решение 0052)."""

import json
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.panel import SIGNATURE_HEADER
from remnabay.panel._webhooks import sign
from remnabay.shop_settings import (
    SettingError,
    check_webhook_secret,
    new_webhook_secret,
    webhook_secret,
)
from remnabay.web.panel_webhook import PANEL_WEBHOOK_PATH
from tests.conftest import REQUIRED_ENV, journaled_in_test
from tests.domain_support import add, make_team_member
from tests.panel_support import user_json
from tests.web_support import Shop, running_shop

BOX = SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])
SETTINGS = "/api/admin/settings/panel"
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
