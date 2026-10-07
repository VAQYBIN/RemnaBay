"""Префикс имён пользователей в панели: задаётся один раз (решение 0057, 1.7)."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.team import TeamMember, TeamRole
from remnabay.panel_names import panel_username
from tests.domain_support import add, make_team_member
from tests.web_support import Shop, running_shop

SETTINGS = "/api/admin/settings/panel"
PREFIX = f"{SETTINGS}/username-prefix"


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


def _prefix_item(body: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = body["items"]
    [item] = [i for i in items if i["key"] == "username_prefix"]
    return item


@pytest.mark.usefixtures("owner")
async def test_0057_default_prefix_is_rb_and_not_locked(shop: Shop) -> None:
    """Решение 0057: пока оператор не сохранил префикс, действует rb, его можно сменить."""
    body = (await shop.http.get(SETTINGS)).json()
    assert (body["username_prefix"], body["username_prefix_locked"]) == ("rb", False)


@pytest.mark.usefixtures("owner")
async def test_0057_prefix_is_saved_once_and_then_locked(shop: Shop) -> None:
    """Решение 0057: префикс сохраняется один раз; изменить его потом нельзя."""
    saved = await shop.http.put(PREFIX, json={"username_prefix": "vpn-shop"})
    assert saved.status_code == 200
    assert (saved.json()["username_prefix"], saved.json()["username_prefix_locked"]) == (
        "vpn-shop",
        True,
    )

    again = await shop.http.put(PREFIX, json={"username_prefix": "other"})
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "locked"
    assert (await shop.http.get(SETTINGS)).json()["username_prefix"] == "vpn-shop"


@pytest.mark.usefixtures("owner")
@pytest.mark.parametrize("value", ["", "a_b", "имя", "x" * 17, "a b"])
async def test_0057_prefix_format_is_checked(shop: Shop, value: str) -> None:
    """Решение 0057: латиница, цифры и дефис, от 1 до 16 символов."""
    response = await shop.http.put(PREFIX, json={"username_prefix": value})
    assert response.status_code == 422


def test_0057_longest_name_fits_panel_limit() -> None:
    """Решение 0057: имя с самым длинным префиксом укладывается в 36 символов панели."""
    assert len(panel_username("x" * 16, 99_999_999_999, 999)) <= 36


@pytest.mark.usefixtures("owner")
async def test_1_7_prefix_is_optional_checklist_item(shop: Shop) -> None:
    """1.7, решение 0057: «Префикс имён в панели» — необязательный пункт чек-листа;
    выполнен, когда префикс зафиксирован."""
    item = _prefix_item((await shop.http.get("/api/admin/checklist")).json())
    assert (item["status"], item["required"]) == ("optional", False)

    await shop.http.put(PREFIX, json={"username_prefix": "vpn"})
    item = _prefix_item((await shop.http.get("/api/admin/checklist")).json())
    assert item["status"] == "done"
    assert item["username_prefix"] == {"prefix": "vpn", "locked": True}


async def test_0057_prefix_is_set_only_by_owner(shop: Shop) -> None:
    """Настройки — только владелец (01-domain)."""
    helper = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, helper)
    await shop.sign_in(helper)
    response = await shop.http.put(PREFIX, json={"username_prefix": "vpn"})
    assert response.status_code == 403
