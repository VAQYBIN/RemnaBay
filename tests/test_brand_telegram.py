"""«Применить в Telegram»: имя, описания и аватар бота из бренда (1.27, решения 0058, 0059)."""

import struct
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyProfilePhoto,
    SetMyShortDescription,
)
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand import AssetKind, TelegramApplyError, check_avatar, save_asset
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Subject, entries_for
from tests.brand_support import make_svg
from tests.domain_support import add
from tests.web_support import Shop, running_shop

URL = "/api/admin/settings/brand/telegram"


def make_jpeg(width: int = 640, height: int = 640) -> bytes:
    """Заголовок JPEG с кадром нужного размера — для проверки формата и размера."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = TeamMember(telegram_id=500, role=TeamRole.OWNER)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


async def _brand(shop: Shop, owner: TeamMember, **fields: str) -> None:
    body = {"name": "Енот VPN", "primary_color": "#1fa4a0", **fields}
    response = await shop.http.put("/api/admin/settings/brand", json=body)
    assert response.status_code == 200
    await save_asset(shop.session, AssetKind.MARK, "image/svg+xml", make_svg(), member_id=owner.id)


async def _apply(shop: Shop, avatar: bytes, background: str = "brand") -> dict[str, Any]:
    response = await shop.http.put(
        URL,
        params={"background": background},
        content=avatar,
        headers={"Content-Type": "image/jpeg"},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_1_27_descriptions_are_brand_settings(shop: Shop, owner: TeamMember) -> None:
    """1.27, 0058: «Описание бота» (до 512) и «Коротко о боте» (до 120) — поля «Бренда»."""
    await _brand(shop, owner, bot_description="Быстрый VPN", bot_short_description="VPN")
    settings = (await shop.http.get("/api/admin/settings/brand")).json()
    assert (settings["bot_description"], settings["bot_short_description"]) == (
        "Быстрый VPN",
        "VPN",
    )
    too_long = await shop.http.put(
        "/api/admin/settings/brand",
        json={"name": "Енот VPN", "primary_color": "#1fa4a0", "bot_short_description": "x" * 121},
    )
    assert too_long.status_code == 422


async def test_1_27_apply_sets_name_descriptions_and_avatar(shop: Shop, owner: TeamMember) -> None:
    """1.27: боту задаются имя (название бренда), описание, «коротко о боте» и аватар;
    результат виден в настройках и записан в журнал."""
    await _brand(shop, owner, bot_description="Быстрый VPN", bot_short_description="VPN")
    avatar = make_jpeg()

    settings = await _apply(shop, avatar, "white")

    requests = shop.telegram.requests
    [name] = [r for r in requests if isinstance(r, SetMyName)]
    [description] = [r for r in requests if isinstance(r, SetMyDescription)]
    [short] = [r for r in requests if isinstance(r, SetMyShortDescription)]
    [photo] = [r for r in requests if isinstance(r, SetMyProfilePhoto)]
    assert (name.name, name.language_code) == ("Енот VPN", None)
    assert description.description == "Быстрый VPN"
    assert short.short_description == "VPN"
    assert photo.photo.type == "static"
    telegram: dict[str, Any] = settings["telegram"]
    assert (telegram["ok"], telegram["background"], telegram["error"]) == (True, "white", None)
    [entry] = await entries_for(shop.session, Subject("telegram_bot", "profile"))
    assert entry.action == "brand.applied_to_telegram"
    assert entry.details["done"] == [
        "name",
        "commands",
        "description",
        "short_description",
        "photo",
    ]


async def test_1_27_empty_fields_do_not_change_telegram(shop: Shop, owner: TeamMember) -> None:
    """1.27: пустое поле в Telegram не меняется."""
    await _brand(shop, owner)
    await _apply(shop, make_jpeg())
    requests = shop.telegram.requests
    assert not [r for r in requests if isinstance(r, SetMyDescription | SetMyShortDescription)]
    assert [r for r in requests if isinstance(r, SetMyName)]


async def test_1_27_telegram_error_is_shown_and_journaled(shop: Shop, owner: TeamMember) -> None:
    """1.27: ошибка Telegram видна в настройках и записана в журнал."""
    await _brand(shop, owner)
    shop.telegram.fail_with = TelegramBadRequest(
        method=SetMyName(name="Енот VPN"), message="Bad Request: too many requests"
    )
    settings = await _apply(shop, make_jpeg())
    telegram: dict[str, Any] = settings["telegram"]
    assert telegram["ok"] is False
    assert "too many requests" in telegram["error"]
    [entry] = await entries_for(shop.session, Subject("telegram_bot", "profile"))
    assert entry.outcome == "failure"


async def test_1_27_needs_name_and_mark(shop: Shop, owner: TeamMember) -> None:
    """1.27: кнопка доступна, когда заданы название и квадратный знак."""
    del owner
    response = await shop.http.put(
        URL,
        params={"background": "brand"},
        content=make_jpeg(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert response.status_code == 409
    assert shop.telegram.requests == []


async def test_1_27_owner_only(shop: Shop) -> None:
    """1.27: применяет владелец."""
    member = TeamMember(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, member)
    await shop.sign_in(member)
    response = await shop.http.put(URL, params={"background": "brand"}, content=make_jpeg())
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (make_svg(), "JPG"),
        (make_jpeg(640, 480), "квадратным"),
        (make_jpeg(100, 100), "от 160"),
    ],
)
def test_1_27_avatar_is_square_jpeg(data: bytes, message: str) -> None:
    """1.27, 0058: Telegram принимает аватар только в JPG; аватар квадратный."""
    with pytest.raises(TelegramApplyError, match=message):
        check_avatar(data)


async def test_0060_apply_sets_command_menu(shop: Shop, owner: TeamMember) -> None:
    """1.27, 0060: «Применить в Telegram» задаёт меню команд — /start и /help, без /admin."""
    await _brand(shop, owner)
    await _apply(shop, make_jpeg())
    [commands] = [r for r in shop.telegram.requests if isinstance(r, SetMyCommands)]
    assert [(c.command, c.description) for c in commands.commands] == [
        ("start", "Главное меню"),
        ("help", "Помощь и инструкция по подключению"),
    ]
