"""Чек-лист первичной настройки и открытие магазина (1.7–1.15, 1.21, 1.22)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import checklist
from remnabay.domain.panel_events import PanelEvent
from remnabay.domain.tariffs import TariffState
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.panel_sync import sync_page
from remnabay.queue._models import QueuePeriodicSlot
from remnabay.shop import SHOP_STATE, ShopState
from tests.brand_support import make_svg
from tests.domain_support import add, make_tariff, make_team_member, put_setting
from tests.web_support import Shop, running_shop

ITEMS = [
    "panel",
    "webhook",
    "device_limit",
    "brand",
    "tariffs",
    "payment",
    "support",
    "trial",
    "migration",
]


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


@pytest.fixture
def payments_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Способ оплаты подключён — провайдеры появятся в блоке 3."""

    async def ready(session: AsyncSession) -> bool:
        del session
        return True

    monkeypatch.setattr(checklist, "payment_methods_ready", ready)


async def _items(shop: Shop) -> dict[str, Any]:
    response = await shop.http.get("/api/admin/checklist")
    assert response.status_code == 200
    return {item["key"]: item for item in response.json()["items"]}


async def _tariff_on_sale(shop: Shop) -> None:
    await add(shop.session, make_tariff(state=TariffState.ON_SALE))


async def test_1_7_checklist_shows_every_item(shop: Shop, owner: TeamMember) -> None:
    """1.7: пока магазин не открыт — чек-лист со всеми пунктами и их состоянием."""
    del owner
    response = await shop.http.get("/api/admin/checklist")

    body = response.json()
    assert body["state"] == "not_opened"
    assert [item["key"] for item in body["items"]] == ITEMS
    statuses = {item["key"]: item["status"] for item in body["items"]}
    assert statuses == {
        "panel": "done",
        "webhook": "todo",
        "device_limit": "done",
        "brand": "todo",
        "tariffs": "todo",
        "payment": "todo",
        "support": "todo",
        "trial": "done",
        "migration": "optional",
    }


async def test_1_7_trial_is_informational(shop: Shop, owner: TeamMember) -> None:
    """1.7: «Триал» всегда выполнен — включён он или нет."""
    del owner
    trial = (await _items(shop))["trial"]

    assert trial["status"] == "done"
    assert trial["trial_enabled"] is False
    assert trial["required"] is False


async def test_1_8_panel_reachable_and_compatible(shop: Shop, owner: TeamMember) -> None:
    del owner
    shop.panel.version = "3.4.7"

    panel = (await _items(shop))["panel"]

    assert panel["status"] == "done"
    assert panel["panel"] == {"version": "3.4.7", "supported_versions": ["3.4.4"], "error": None}


async def test_1_8_incompatible_version_shows_required(shop: Shop, owner: TeamMember) -> None:
    """1.8: несовместимая версия — показывается требуемая."""
    del owner
    shop.panel.version = "3.5.0"

    panel = (await _items(shop))["panel"]

    assert panel["status"] == "todo"
    assert panel["panel"] == {
        "version": "3.5.0",
        "supported_versions": ["3.4.4"],
        "error": "incompatible_version",
    }


@pytest.mark.parametrize(
    ("down", "error_status", "error"),
    [(True, None, "unavailable"), (False, 401, "unauthorized"), (False, 500, "error")],
)
async def test_1_8_panel_not_answering(
    shop: Shop, owner: TeamMember, down: bool, error_status: int | None, error: str
) -> None:
    """1.8: API панели не отвечает с токеном из .env — пункт не выполнен."""
    del owner
    shop.panel.down = down
    shop.panel.error_status = error_status

    items = await _items(shop)

    assert items["panel"]["status"] == "todo"
    assert items["panel"]["panel"]["error"] == error
    assert items["device_limit"]["status"] == "todo"


async def test_1_9_webhook_address_and_secret(shop: Shop, owner: TeamMember) -> None:
    """1.9: адрес и секрет для панели; выполнен после первого события с верной подписью."""
    del owner
    webhook = (await _items(shop))["webhook"]
    assert webhook["status"] == "todo"
    assert webhook["webhook"]["url"] == "https://shop.example.com/webhooks/panel"
    assert len(webhook["webhook"]["secret"]) > 30

    await add(
        shop.session,
        PanelEvent(body_hash=b"0" * 32, event="user.modified", payload={}),
    )

    assert (await _items(shop))["webhook"]["status"] == "done"


async def test_1_9_assistant_does_not_see_secret(shop: Shop) -> None:
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)

    webhook = (await _items(shop))["webhook"]

    assert webhook["webhook"]["secret"] is None


@pytest.mark.parametrize("hwid", [False, None])
async def test_1_10_device_limit_off_is_warning(
    shop: Shop, owner: TeamMember, hwid: bool | None
) -> None:
    """1.10: лимит устройств выключен — предупреждение о защите триала."""
    del owner
    shop.panel.hwid_enabled = hwid

    item = (await _items(shop))["device_limit"]

    assert item["status"] == "warning"
    assert item["required"] is False


@pytest.mark.usefixtures("payments_ready")
async def test_1_10_device_limit_off_does_not_block_opening(shop: Shop, owner: TeamMember) -> None:
    del owner
    shop.panel.hwid_enabled = False
    await _tariff_on_sale(shop)

    response = await shop.http.post("/api/admin/shop/open")

    assert response.status_code == 200
    assert response.json()["state"] == "open"


async def test_1_11_brand_needs_name_and_mark(shop: Shop, owner: TeamMember) -> None:
    """1.11: «Бренд» выполнен, когда заданы название и квадратный знак."""
    del owner
    await shop.http.put(
        "/api/admin/settings/brand", json={"name": "Енот VPN", "primary_color": "#1FA4A0"}
    )
    assert (await _items(shop))["brand"]["status"] == "todo"

    await shop.http.put(
        "/api/admin/settings/brand/assets/mark",
        content=make_svg(),
        headers={"Content-Type": "image/svg+xml"},
    )

    assert (await _items(shop))["brand"]["status"] == "done"


async def test_1_12_migration_is_optional(shop: Shop, owner: TeamMember) -> None:
    del owner
    migration = (await _items(shop))["migration"]

    assert migration["status"] == "optional"
    assert migration["required"] is False


async def test_1_14_open_requires_mandatory_items(shop: Shop, owner: TeamMember) -> None:
    """1.14: без подключения к панели, тарифа в продаже и способа оплаты — не открывается."""
    del owner
    shop.panel.down = True

    response = await shop.http.post("/api/admin/shop/open")

    assert response.status_code == 409
    assert response.json()["detail"] == {"missing": ["panel", "tariffs", "payment"]}
    body = (await shop.http.get("/api/admin/checklist")).json()
    assert body["can_open"] is False
    assert body["state"] == "not_opened"


@pytest.mark.usefixtures("payments_ready")
async def test_1_14_owner_opens_shop(shop: Shop, owner: TeamMember) -> None:
    """1.14: обязательные пункты выполнены — кнопка доступна, магазин открывает владелец."""
    del owner
    await _tariff_on_sale(shop)
    assert (await shop.http.get("/api/admin/checklist")).json()["can_open"] is True

    response = await shop.http.post("/api/admin/shop/open")

    assert response.json()["state"] == "open"
    assert response.json()["can_open"] is False


@pytest.mark.usefixtures("payments_ready")
async def test_1_14_archived_tariff_does_not_count(shop: Shop, owner: TeamMember) -> None:
    del owner
    await add(shop.session, make_tariff(state=TariffState.ARCHIVED))

    assert (await _items(shop))["tariffs"]["status"] == "todo"


@pytest.mark.usefixtures("payments_ready")
async def test_1_14_assistant_cannot_open(shop: Shop) -> None:
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)
    await _tariff_on_sale(shop)

    assert (await shop.http.post("/api/admin/shop/open")).status_code == 403


@pytest.mark.usefixtures("payments_ready")
async def test_1_15_checklist_complete_only_without_open_items(
    shop: Shop, owner: TeamMember
) -> None:
    """1.15: чек-лист остаётся на главной, пока есть невыполненные пункты."""
    del owner
    await _tariff_on_sale(shop)
    await shop.http.post("/api/admin/shop/open")
    assert (await shop.http.get("/api/admin/checklist")).json()["complete"] is False

    await shop.http.put(
        "/api/admin/settings/brand", json={"name": "Енот VPN", "primary_color": "#1FA4A0"}
    )
    await shop.http.put(
        "/api/admin/settings/brand/assets/mark",
        content=make_svg(),
        headers={"Content-Type": "image/svg+xml"},
    )
    await shop.http.put(
        "/api/admin/settings/shop",
        json={"support_contact": "@support", "time_zone": "Europe/Moscow", "testers": []},
    )
    await add(shop.session, PanelEvent(body_hash=b"1" * 32, event="user.modified", payload={}))

    assert (await shop.http.get("/api/admin/checklist")).json()["complete"] is True


@pytest.mark.usefixtures("payments_ready")
async def test_1_22_owner_closes_and_reopens_shop(shop: Shop, owner: TeamMember) -> None:
    """1.22: владелец может закрыть открытый магазин и открыть снова."""
    del owner
    await _tariff_on_sale(shop)
    await shop.http.post("/api/admin/shop/open")

    closed = await shop.http.post("/api/admin/shop/close")
    reopened = await shop.http.post("/api/admin/shop/open")

    assert closed.json()["state"] == "paused"
    assert reopened.json()["state"] == "open"


async def test_1_22_close_does_nothing_before_opening(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.post("/api/admin/shop/close")

    assert response.json()["state"] == "not_opened"


async def test_1_21_testers_and_support_in_shop_settings(shop: Shop, owner: TeamMember) -> None:
    """1.21: владелец задаёт тестировщиков; повторы убираются."""
    del owner
    response = await shop.http.put(
        "/api/admin/settings/shop",
        json={
            "support_contact": " @help ",
            "time_zone": "Asia/Yekaterinburg",
            "testers": [7, 8, 7],
        },
    )

    assert response.json() == {
        "support_contact": "@help",
        "time_zone": "Asia/Yekaterinburg",
        "testers": [7, 8],
    }
    assert (await _items(shop))["support"]["status"] == "done"


async def test_shop_settings_reject_unknown_time_zone(shop: Shop, owner: TeamMember) -> None:
    del owner
    response = await shop.http.put(
        "/api/admin/settings/shop",
        json={"support_contact": "", "time_zone": "Mars/Olympus", "testers": []},
    )

    assert response.status_code == 422


async def test_1_5_login_settings(shop: Shop, owner: TeamMember) -> None:
    """1.5: срок действия подтверждения входа — настройка «Вход в админку»."""
    del owner
    assert (await shop.http.get("/api/admin/settings/login")).json() == {
        "login_ttl_minutes": 5,
        "session_ttl_days": 30,
    }

    response = await shop.http.put(
        "/api/admin/settings/login", json={"login_ttl_minutes": 10, "session_ttl_days": 7}
    )

    assert response.json() == {"login_ttl_minutes": 10, "session_ttl_days": 7}


async def test_overview_shows_links_and_attention(shop: Shop, owner: TeamMember) -> None:
    """А1: состояние связей и счётчики «Требуют внимания» (4.20, 4.30) на главной."""
    del owner
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    received = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    await add(
        shop.session,
        PanelEvent(body_hash=b"2" * 32, event="user.modified", payload={}, received_at=received),
    )

    body = (await shop.http.get("/api/admin/overview")).json()

    assert body["state"] == "paused"
    assert body["panel_available"] is True
    assert body["last_panel_event_at"] == "2026-10-06T09:00:00Z"
    assert body["attention"] == 0
    assert body["waiting_panel"] == 0
    assert body["last_sync_at"] is None


async def test_overview_shows_last_sync_start(shop: Shop, owner: TeamMember) -> None:
    """А1: когда последний раз запускалась сверка с панелью (4.8)."""
    del owner
    started = datetime(2026, 10, 6, 8, 45, tzinfo=UTC)
    await add(shop.session, QueuePeriodicSlot(name=sync_page.name, slot_start=started))

    body = (await shop.http.get("/api/admin/overview")).json()

    assert body["last_sync_at"] == "2026-10-06T08:45:00Z"
