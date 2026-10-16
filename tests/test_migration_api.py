"""Миграция в админке (экран А9, 9.1, 9.3, 9.8, 9.10): только владелец, пробный прогон,
применение, история."""

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.team import TeamMember, TeamRole
from remnabay.shop import SHOP_STATE, ShopState
from tests.domain_support import add, put_setting
from tests.migration_support import run_chain
from tests.panel_support import fake_runtime
from tests.web_support import Shop, running_shop


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        yield shop


async def _sign_in(shop: Shop, role: TeamRole = TeamRole.OWNER) -> TeamMember:
    member = TeamMember(telegram_id=1 if role == TeamRole.OWNER else 2, role=role)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


async def _chain(shop: Shop) -> None:
    with fake_runtime(panel=shop.panel):
        await run_chain(shop.session)


def _file(*clients: dict[str, Any], version: int = 1) -> bytes:
    raw = {"format_version": version, "source": {"name": "test-bot"}, "clients": list(clients)}
    return json.dumps(raw).encode()


async def test_9_1_only_owner(shop: Shop) -> None:
    """9.1, 01-domain: миграция — только владельцу; помощник получает отказ."""
    await _sign_in(shop, TeamRole.ASSISTANT)

    assert (await shop.http.get("/api/admin/migration/runs")).status_code == 403
    assert (await shop.http.post("/api/admin/migration/adoption", json={})).status_code == 403
    response = await shop.http.post("/api/admin/migration/import", content=_file())
    assert response.status_code == 403


async def test_9_1_9_3_adoption_dry_run_then_apply(shop: Shop) -> None:
    """9.1, 9.3: сначала пробный прогон; «Применить» — у последнего пробного; после
    применения — итоговый отчёт, применить второй раз нельзя."""
    await _sign_in(shop)
    shop.panel.add_user(id=11, shortUuid="s11", username="tg_1001", telegramId=1001)

    started = await shop.http.post("/api/admin/migration/adoption", json={"mapping": []})
    assert started.status_code == 202, started.text
    assert started.json()["state"] == "reading_panel"
    busy = await shop.http.post("/api/admin/migration/adoption", json={})
    assert busy.status_code == 409
    assert busy.json()["detail"]["reason"] == "busy"

    await _chain(shop)
    [dry] = (await shop.http.get("/api/admin/migration/runs")).json()["runs"]
    assert (dry["state"], dry["can_apply"], dry["report"]["adopt"]) == ("done", True, 1)

    applied = await shop.http.post(f"/api/admin/migration/runs/{dry['id']}/apply")
    assert applied.status_code == 202, applied.text
    await _chain(shop)
    runs = (await shop.http.get("/api/admin/migration/runs")).json()["runs"]
    final, dry_after = runs
    assert (final["dry_run"], final["state"], final["report"]["adopt"]) == (False, "done", 1)
    assert (dry_after["can_apply"], dry_after["applied_by_run_id"]) == (False, final["id"])
    again = await shop.http.post(f"/api/admin/migration/runs/{dry['id']}/apply")
    assert again.status_code == 409
    assert shop.panel.writes == []


async def test_9_8_9_10_import_file(shop: Shop) -> None:
    """9.8, 9.10: файл загружается телом запроса и сразу идёт пробный прогон; файл
    неизвестной версии отклоняется с понятной причиной."""
    await _sign_in(shop)

    wrong = await shop.http.post("/api/admin/migration/import", content=_file(version=7))
    assert wrong.status_code == 422
    assert "Поддерживаемые версии: 1" in wrong.json()["detail"]["message"]

    started = await shop.http.post(
        "/api/admin/migration/import", content=_file({"telegram_id": 3001, "trial_used": True})
    )
    assert started.status_code == 202, started.text
    await _chain(shop)
    run = (await shop.http.get(f"/api/admin/migration/runs/{started.json()['id']}")).json()
    assert (run["kind"], run["source"]["name"]) == ("import", "test-bot")
    assert run["report"]["file"]["without_panel_user"] == 1


async def test_9_15_rerun_with_mapping(shop: Shop) -> None:
    """9.15: пробный прогон заново с сопоставлением; неизвестный тариф — отказ."""
    await _sign_in(shop)
    started = await shop.http.post("/api/admin/migration/adoption", json={})
    await _chain(shop)
    rule: dict[str, Any] = {"squads": [], "device_limit": 3, "tariff_id": 999_999}

    response = await shop.http.post(
        f"/api/admin/migration/runs/{started.json()['id']}/rerun", json={"mapping": [rule]}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_tariff"
