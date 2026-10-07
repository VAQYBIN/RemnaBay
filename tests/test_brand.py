"""Бренд оператора: название, логотипы, цвета, приветственный текст (1.11, 1.23, 1.24)."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand import (
    MAX_ASSET_BYTES,
    PNG,
    SVG,
    AssetError,
    AssetKind,
    validate_asset,
)
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from tests.brand_support import make_png, make_svg
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_team_member
from tests.web_support import Shop, running_shop

BRAND = "/api/admin/brand"
SETTINGS = "/api/admin/settings/brand"
DEFAULT_WELCOME = "Быстрый и надёжный VPN. Подключение занимает пару минут."


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


def _settings(**overrides: object) -> dict[str, object]:
    return {"name": "Енот VPN", "primary_color": "#7C3AED", **overrides}


async def test_1_23_remnabay_until_brand_set(shop: Shop) -> None:
    """1.23: пока название и знак не заданы — название и знак RemnaBay; без входа."""
    response = await shop.http.get(BRAND)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "RemnaBay"
    assert body["name_is_default"] is True
    assert body["mark_url"] is None
    assert body["tokens"]["dark"]["bg"].startswith("#")


async def test_1_23_brand_change_visible_without_restart(shop: Shop, owner: TeamMember) -> None:
    """1.23: изменение бренда видно сразу — без перезапуска."""
    del owner
    before = (await shop.http.get(BRAND)).json()

    saved = await shop.http.put(SETTINGS, json=_settings())
    upload = await shop.http.put(
        f"{SETTINGS}/assets/mark", content=make_svg(), headers={"Content-Type": SVG}
    )

    assert saved.status_code == 200
    assert upload.status_code == 200
    after = (await shop.http.get(BRAND)).json()
    assert after["name"] == "Енот VPN"
    assert after["name_is_default"] is False
    assert after["mark_url"].startswith("/brand/mark?v=")
    assert after["tokens"]["light"]["primary"] != before["tokens"]["light"]["primary"]


async def test_1_23_mark_is_served_with_cache_and_without_scripts(
    shop: Shop, owner: TeamMember
) -> None:
    del owner
    await shop.http.put(
        f"{SETTINGS}/assets/mark", content=make_svg(), headers={"Content-Type": SVG}
    )
    url = (await shop.http.get(BRAND)).json()["mark_url"]

    response = await shop.http.get(url)

    assert response.content == make_svg()
    assert response.headers["content-type"] == SVG
    assert "immutable" in response.headers["cache-control"]
    assert "sandbox" in response.headers["content-security-policy"]


async def test_1_11_settings_and_journal(shop: Shop, owner: TeamMember) -> None:
    """1.11: название, цвета и приветственный текст сохраняются; каждое изменение — в журнале."""
    response = await shop.http.put(
        SETTINGS,
        json=_settings(secondary_color="#E9B872", welcome_text="Привет от енота!"),
    )

    body = response.json()
    assert body["name"] == "Енот VPN"
    assert body["primary_color"] == "#7c3aed"
    assert body["secondary_color"] == "#e9b872"
    assert body["welcome_text"] == "Привет от енота!"
    assert body["welcome_text_is_default"] is False
    actions = await shop.session.scalars(
        select(JournalEntry.action).where(
            journaled_in_test(), JournalEntry.actor_ref == str(owner.id)
        )
    )
    assert sorted(actions) == sorted(
        ["team.login", "setting.changed", "setting.changed", "setting.changed", "text.changed"]
    )


async def test_1_11_welcome_text_resets_to_default(shop: Shop, owner: TeamMember) -> None:
    del owner
    await shop.http.put(SETTINGS, json=_settings(welcome_text="Свой текст"))

    response = await shop.http.put(SETTINGS, json=_settings(welcome_text=""))

    assert response.json()["welcome_text"] == DEFAULT_WELCOME
    assert response.json()["welcome_text_is_default"] is True


async def test_1_11_welcome_text_rejects_unknown_variable(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.put(SETTINGS, json=_settings(welcome_text="Привет, {balance}"))

    assert response.status_code == 422


async def test_1_24_invalid_color_rejected(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.put(SETTINGS, json=_settings(primary_color="teal"))

    assert response.status_code == 422


async def test_1_24_preview_shows_result_and_warning(shop: Shop, owner: TeamMember) -> None:
    """1.24: видно, каким цвет станет; близость к смысловому — предупреждение."""
    del owner
    response = await shop.http.post(f"{SETTINGS}/preview", json={"primary_color": "#22C55E"})

    body = response.json()
    assert body["warnings"] == ["success"]
    assert body["adjusted"] is True
    assert body["light_primary"] != "#22c55e"


async def test_settings_are_owner_only(shop: Shop) -> None:
    """Настройки бренда — только владельцу (01-domain, таблица ролей)."""
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)

    assert (await shop.http.get(SETTINGS)).status_code == 403
    assert (await shop.http.put(SETTINGS, json=_settings())).status_code == 403


async def test_settings_require_login(shop: Shop) -> None:
    assert (await shop.http.get(SETTINGS)).status_code == 401


async def test_1_11_asset_upload_rejects_bad_file(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.put(
        f"{SETTINGS}/assets/mark", content=make_png(256, 256), headers={"Content-Type": PNG}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Квадратный знак — не меньше 512×512 пикселей"


async def test_1_11_asset_too_large(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.put(
        f"{SETTINGS}/assets/logo",
        content=b"0" * (MAX_ASSET_BYTES + 1),
        headers={"Content-Type": PNG},
    )

    assert response.status_code == 413


async def test_1_11_asset_can_be_removed(shop: Shop, owner: TeamMember) -> None:
    del owner
    await shop.http.put(
        f"{SETTINGS}/assets/logo", content=make_svg(300, 80), headers={"Content-Type": SVG}
    )

    response = await shop.http.delete(f"{SETTINGS}/assets/logo")

    assert response.json()["logo"] is None


@pytest.mark.parametrize(
    ("kind", "content_type", "data"),
    [
        (AssetKind.MARK, PNG, make_png(512, 512)),
        (AssetKind.MARK, PNG, make_png(1024, 1024)),
        (AssetKind.MARK, SVG, make_svg(64, 64)),
        (AssetKind.LOGO, SVG, make_svg(300, 80)),
        (AssetKind.LOGO, PNG, make_png(600, 160)),
    ],
)
def test_1_11_accepted_logos(kind: AssetKind, content_type: str, data: bytes) -> None:
    validate_asset(kind, content_type, data)


@pytest.mark.parametrize(
    ("kind", "content_type", "data", "message"),
    [
        (AssetKind.MARK, PNG, make_png(511, 511), "не меньше 512×512"),
        (AssetKind.MARK, PNG, make_png(800, 600), "квадратным"),
        (AssetKind.MARK, SVG, make_svg(300, 80), "квадратным"),
        (AssetKind.MARK, PNG, make_png(512, 512, alpha=False), "прозрачным фоном"),
        (AssetKind.LOGO, "image/jpeg", b"\xff\xd8\xff", "SVG или PNG"),
        (AssetKind.LOGO, PNG, b"not a png", "не похож на PNG"),
        (AssetKind.LOGO, PNG, make_png(600, 160)[:20], "не похож на PNG"),
        (AssetKind.LOGO, SVG, b"<html></html>", "не похож на SVG"),
        (AssetKind.LOGO, SVG, b"<svg", "не похож на SVG"),
        (
            AssetKind.LOGO,
            SVG,
            b'<!DOCTYPE svg [<!ENTITY a "x">]><svg viewBox="0 0 1 1">&a;</svg>',
            "DOCTYPE",
        ),
    ],
)
def test_1_11_rejected_logos(kind: AssetKind, content_type: str, data: bytes, message: str) -> None:
    with pytest.raises(AssetError, match=message):
        validate_asset(kind, content_type, data)
