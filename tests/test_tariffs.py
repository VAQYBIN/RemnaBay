"""Тарифы и их архивация: API админки и сервис (блок 2, решение 0054)."""

from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment
from remnabay.domain.tariffs import Tariff, TariffState, TariffType
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import ActorType, JournalEntry
from remnabay.tariffs import tariffs_on_sale
from tests.conftest import journaled_in_test
from tests.domain_support import (
    GB,
    add,
    make_client,
    make_payment,
    make_subscription,
    make_tariff,
    make_team_member,
)
from tests.web_support import Shop, running_shop

TARIFFS = "/api/admin/tariffs"
SQUADS = "/api/admin/panel/squads"
GERMANY = str(uuid4())
FINLAND = str(uuid4())


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        shop.panel.squads = {GERMANY: "Германия", FINLAND: "Финляндия"}
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = make_team_member(telegram_id=500, role=TeamRole.OWNER)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Месяц",
        "description": "Все серверы, без ограничения трафика",
        "duration_days": 30,
        "price": "199.00",
        "device_limit": 3,
        "squad_uuids": [GERMANY],
    }
    body.update(overrides)
    return body


async def _create(shop: Shop, **overrides: Any) -> dict[str, Any]:
    response = await shop.http.post(TARIFFS, json=_body(**overrides))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def _list(shop: Shop) -> list[dict[str, Any]]:
    response = await shop.http.get(TARIFFS)
    assert response.status_code == 200
    tariffs: list[dict[str, Any]] = response.json()["tariffs"]
    return tariffs


async def _journal(session: AsyncSession, action: str) -> list[JournalEntry]:
    result = await session.scalars(
        select(JournalEntry)
        .where(journaled_in_test(), JournalEntry.action == action)
        .order_by(JournalEntry.id)
    )
    return list(result)


async def _names_on_sale(session: AsyncSession) -> list[str]:
    return [tariff.name for tariff in await tariffs_on_sale(session)]


# --- 2.1–2.3: создание ---


@pytest.mark.usefixtures("owner")
async def test_2_1_owner_creates_term_unlimited_tariff(shop: Shop) -> None:
    """2.1, 2.3: название, описание, срок, цена, лимит устройств, сквады;
    тип — «срок + безлимит»."""
    created = await _create(shop, squad_uuids=[GERMANY, FINLAND])

    tariff = await shop.session.get(Tariff, created["id"])
    assert tariff is not None
    assert tariff.name == "Месяц"
    assert tariff.description == "Все серверы, без ограничения трафика"
    assert tariff.type == TariffType.TERM_UNLIMITED
    assert tariff.state == TariffState.ON_SALE
    assert tariff.duration_days == 30
    assert tariff.price == Decimal("199.00")
    assert tariff.device_limit == 3
    assert [str(squad) for squad in tariff.squad_uuids] == [GERMANY, FINLAND]
    assert tariff.traffic_limit_bytes is None
    assert created["type"] == "term_unlimited"
    assert created["in_use"] is False


async def test_4_25_tariff_creation_is_journaled(shop: Shop, owner: TeamMember) -> None:
    """4.25: создание тарифа — ручное действие команды, оно в журнале."""
    created = await _create(shop)

    [entry] = await _journal(shop.session, "tariff.created")
    assert entry.actor_type == ActorType.TEAM_MEMBER
    assert entry.actor_ref == str(owner.id)
    assert (entry.subject_type, entry.subject_id) == ("tariff", str(created["id"]))
    assert entry.details["price"] == "199.00"


@pytest.mark.usefixtures("owner")
@pytest.mark.parametrize(
    "overrides",
    [
        {"device_limit": 0},
        {"device_limit": None},
        {"squad_uuids": []},
        {"price": "0"},
        {"price": "199.001"},
        {"duration_days": 0},
        {"name": "   "},
    ],
    ids=[
        "no_device_limit",
        "device_limit_missing",
        "no_squads",
        "free",
        "fraction_of_kopeck",
        "zero_duration",
        "blank_name",
    ],
)
async def test_2_1_invalid_tariff_is_rejected(shop: Shop, overrides: dict[str, Any]) -> None:
    """2.1: лимит устройств не меньше 1 (0054); сквад, цена, срок и название обязательны."""
    response = await shop.http.post(TARIFFS, json=_body(**overrides))

    assert response.status_code == 422
    assert await shop.session.scalar(select(Tariff.id)) is None


@pytest.mark.usefixtures("owner")
@pytest.mark.parametrize("tariff_type", ["term_quota", "term_package", "package_only"])
async def test_2_3_only_term_unlimited_is_available(shop: Shop, tariff_type: str) -> None:
    """2.3: в MVP в админке доступен только тип «срок + безлимит»."""
    response = await shop.http.post(TARIFFS, json=_body(type=tariff_type))

    assert response.status_code == 422


# --- 2.2: сквады из панели ---


@pytest.mark.usefixtures("owner")
async def test_2_2_squads_come_from_panel(shop: Shop) -> None:
    """2.2: список сквадов для формы тарифа — из панели."""
    response = await shop.http.get(SQUADS)

    assert response.status_code == 200
    assert response.json() == [
        {"uuid": GERMANY, "name": "Германия"},
        {"uuid": FINLAND, "name": "Финляндия"},
    ]


@pytest.mark.usefixtures("owner")
async def test_2_2_squad_not_in_panel_is_rejected(shop: Shop) -> None:
    """2.2: сквад, которого нет в панели, выбрать нельзя."""
    response = await shop.http.post(TARIFFS, json=_body(squad_uuids=[GERMANY, str(uuid4())]))

    assert response.status_code == 422
    assert "нет в панели" in response.json()["detail"]
    assert await shop.session.scalar(select(Tariff.id)) is None


@pytest.mark.usefixtures("owner")
async def test_2_2_panel_down_squads_unavailable(shop: Shop) -> None:
    """2.2: панель недоступна — список не получить, новый тариф не сохранить."""
    shop.panel.down = True

    assert (await shop.http.get(SQUADS)).status_code == 503
    assert (await shop.http.post(TARIFFS, json=_body())).status_code == 503


@pytest.mark.usefixtures("owner")
async def test_2_2_edit_without_new_squads_does_not_need_panel(shop: Shop) -> None:
    """2.2: правка без новых сквадов не зависит от панели — проверяются только новые."""
    created = await _create(shop)
    shop.panel.down = True

    response = await shop.http.put(f"{TARIFFS}/{created['id']}", json=_body(price="249.00"))
    assert response.status_code == 200
    assert response.json()["price"] == "249.00"

    with_new = _body(squad_uuids=[GERMANY, FINLAND])
    assert (await shop.http.put(f"{TARIFFS}/{created['id']}", json=with_new)).status_code == 503


# --- 2.4: порядок ---


@pytest.mark.usefixtures("owner")
async def test_2_4_owner_sets_order_seen_by_client(shop: Shop) -> None:
    """2.4: новые тарифы встают в конец; владелец задаёт порядок, его видит клиент."""
    month = await _create(shop, name="Месяц")
    quarter = await _create(shop, name="3 месяца", duration_days=90)
    year = await _create(shop, name="Год", duration_days=365)
    assert await _names_on_sale(shop.session) == ["Месяц", "3 месяца", "Год"]

    response = await shop.http.put(
        f"{TARIFFS}/order", json={"tariff_ids": [year["id"], month["id"], quarter["id"]]}
    )

    assert response.status_code == 200
    assert [tariff["name"] for tariff in response.json()["tariffs"]] == ["Год", "Месяц", "3 месяца"]
    assert await _names_on_sale(shop.session) == ["Год", "Месяц", "3 месяца"]
    [entry] = await _journal(shop.session, "tariff.reordered")
    assert entry.details["new"] == [year["id"], month["id"], quarter["id"]]


@pytest.mark.usefixtures("owner")
async def test_2_4_order_from_stale_screen_is_rejected(shop: Shop) -> None:
    """2.4: порядок задаётся для всех тарифов в продаже; устаревший список отклоняется."""
    month = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)

    response = await shop.http.put(f"{TARIFFS}/order", json={"tariff_ids": [month["id"]]})

    assert response.status_code == 422
    assert await _names_on_sale(shop.session) == ["Месяц", "Год"]


@pytest.mark.usefixtures("owner")
async def test_2_4_list_shows_on_sale_first_then_archive(shop: Shop) -> None:
    """А6: список в порядке показа клиентам, затем архив."""
    month = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)
    await shop.http.post(f"{TARIFFS}/{month['id']}/archive", json={})

    tariffs = await _list(shop)

    assert [(tariff["name"], tariff["state"]) for tariff in tariffs] == [
        ("Год", "on_sale"),
        ("Месяц", "archived"),
    ]


# --- 2.5, 2.6: правка не трогает подписки и счета ---


@pytest.mark.usefixtures("owner")
async def test_2_5_editing_tariff_does_not_touch_subscriptions(shop: Shop) -> None:
    """2.5: правка параметров не меняет действующие подписки и ничего не пишет в панель."""
    created = await _create(shop)
    tariff = await shop.session.get(Tariff, created["id"])
    client = await make_client(shop.session)
    subscription = make_subscription(client, tariff)
    await add(shop.session, subscription)
    shop.panel.requests.clear()

    response = await shop.http.put(
        f"{TARIFFS}/{created['id']}",
        json=_body(name="Месяц+", duration_days=31, device_limit=5, squad_uuids=[FINLAND]),
    )

    assert response.status_code == 200
    await shop.session.refresh(subscription)
    assert subscription.tariff_id == created["id"]
    # Запрос в панель — только список сквадов для проверки нового сквада (2.2)
    assert shop.panel.requests == ["GET /api/internal-squads"]
    [entry] = await _journal(shop.session, "tariff.changed")
    assert entry.details == {
        "name": ["Месяц", "Месяц+"],
        "duration_days": [30, 31],
        "device_limit": [3, 5],
        "squad_uuids": [[GERMANY], [FINLAND]],
    }


@pytest.mark.usefixtures("owner")
async def test_2_6_price_change_does_not_affect_created_invoices(shop: Shop) -> None:
    """2.6: новая цена не меняет уже созданный счёт (3.7)."""
    created = await _create(shop)
    tariff = await shop.session.get(Tariff, created["id"])
    client = await make_client(shop.session)
    payment = make_payment(client, tariff)
    await add(shop.session, payment)

    response = await shop.http.put(f"{TARIFFS}/{created['id']}", json=_body(price="299.00"))

    assert response.status_code == 200
    await shop.session.refresh(payment)
    assert payment.amount == Decimal("199.00")
    assert payment.tariff_snapshot == {"price": "199.00", "duration_days": 30}


@pytest.mark.usefixtures("owner")
async def test_2_5_saving_unchanged_tariff_writes_nothing(shop: Shop) -> None:
    """4.25: сохранение без изменений — не изменение, журнал его не пишет."""
    created = await _create(shop)

    response = await shop.http.put(f"{TARIFFS}/{created['id']}", json=_body())

    assert response.status_code == 200
    assert await _journal(shop.session, "tariff.changed") == []


@pytest.mark.usefixtures("owner")
async def test_2_3_other_types_are_not_edited_in_mvp(shop: Shop) -> None:
    """2.3: тариф другого типа (данные — MVP) в админке MVP не редактируется."""
    package = make_tariff(type=TariffType.TERM_PACKAGE, traffic_limit_bytes=50 * GB)
    await add(shop.session, package)

    response = await shop.http.put(f"{TARIFFS}/{package.id}", json=_body())

    assert response.status_code == 422


# --- 2.7, 2.9: архив ---


@pytest.mark.usefixtures("owner")
async def test_2_7_archive_and_return_to_sale(shop: Shop) -> None:
    """2.7: архивный тариф не продаётся; вернувшийся в продажу встаёт в конец списка."""
    month = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)

    archived = await shop.http.post(f"{TARIFFS}/{month['id']}/archive", json={})
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    assert await _names_on_sale(shop.session) == ["Год"]

    restored = await shop.http.post(f"{TARIFFS}/{month['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["state"] == "on_sale"
    assert await _names_on_sale(shop.session) == ["Год", "Месяц"]

    assert len(await _journal(shop.session, "tariff.archived")) == 1
    assert len(await _journal(shop.session, "tariff.restored")) == 1


@pytest.mark.usefixtures("owner")
async def test_2_9_archiving_last_on_sale_needs_confirmation(shop: Shop) -> None:
    """2.9: архивация последнего тарифа в продаже — только после предупреждения."""
    month = await _create(shop)

    warned = await shop.http.post(f"{TARIFFS}/{month['id']}/archive", json={})
    assert warned.status_code == 409
    assert warned.json()["detail"]["reason"] == "last_on_sale"
    assert "не смогут ничего купить" in warned.json()["detail"]["message"]
    assert await _names_on_sale(shop.session) == ["Месяц"]

    confirmed = await shop.http.post(
        f"{TARIFFS}/{month['id']}/archive", json={"confirm_last": True}
    )
    assert confirmed.status_code == 200
    assert await _names_on_sale(shop.session) == []


@pytest.mark.usefixtures("owner")
async def test_2_9_archiving_one_of_several_needs_no_confirmation(shop: Shop) -> None:
    """2.9: пока в продаже остаются другие тарифы, предупреждения нет."""
    month = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)

    response = await shop.http.post(f"{TARIFFS}/{month['id']}/archive", json={})

    assert response.status_code == 200


# --- 2.8: удаление ---


@pytest.mark.usefixtures("owner")
async def test_2_8_unused_tariff_is_deleted(shop: Shop) -> None:
    """2.8: тариф без подписок удаляется; удаление — в журнале."""
    month = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)

    response = await shop.http.delete(f"{TARIFFS}/{month['id']}")

    assert response.status_code == 204
    assert await shop.session.get(Tariff, month["id"]) is None
    [entry] = await _journal(shop.session, "tariff.deleted")
    assert entry.subject_id == str(month["id"])
    assert entry.details["name"] == "Месяц"


@pytest.mark.usefixtures("owner")
async def test_2_8_tariff_with_subscriptions_is_only_archived(shop: Shop) -> None:
    """2.8: тариф, у которого есть или были подписки, удалить нельзя — только архивировать."""
    created = await _create(shop, name="Месяц")
    await _create(shop, name="Год", duration_days=365)
    tariff = await shop.session.get(Tariff, created["id"])
    client = await make_client(shop.session)
    await add(shop.session, make_subscription(client, tariff))

    response = await shop.http.delete(f"{TARIFFS}/{created['id']}")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "in_use"
    assert response.json()["detail"]["usage"] == ["subscriptions"]
    assert await shop.session.get(Tariff, created["id"]) is not None
    assert {t["name"]: t["in_use"] for t in await _list(shop)} == {"Месяц": True, "Год": False}


@pytest.mark.usefixtures("owner")
async def test_2_8_tariff_with_invoice_is_only_archived(shop: Shop) -> None:
    """2.8: по тарифу открыт счёт — его оплатят и применят к тарифу, удалять нельзя."""
    created = await _create(shop)
    await _create(shop, name="Год", duration_days=365)
    tariff = await shop.session.get(Tariff, created["id"])
    client = await make_client(shop.session)
    await add(shop.session, make_payment(client, tariff))

    response = await shop.http.delete(f"{TARIFFS}/{created['id']}")

    assert response.status_code == 409
    assert response.json()["detail"]["usage"] == ["payments"]
    assert await shop.session.scalar(select(Payment.tariff_id)) == created["id"]


@pytest.mark.usefixtures("owner")
async def test_2_9_deleting_last_on_sale_needs_confirmation(shop: Shop) -> None:
    """2.9: удаление последнего тарифа в продаже — тоже после предупреждения."""
    month = await _create(shop)

    warned = await shop.http.delete(f"{TARIFFS}/{month['id']}")
    assert warned.status_code == 409
    assert warned.json()["detail"]["reason"] == "last_on_sale"

    confirmed = await shop.http.delete(f"{TARIFFS}/{month['id']}?confirm_last=true")
    assert confirmed.status_code == 204


@pytest.mark.usefixtures("owner")
async def test_2_8_archived_unused_tariff_is_deleted_without_warning(shop: Shop) -> None:
    """2.8, 2.9: архивный тариф не в продаже — удаление без предупреждения."""
    month = await _create(shop)
    await shop.http.post(f"{TARIFFS}/{month['id']}/archive", json={"confirm_last": True})

    response = await shop.http.delete(f"{TARIFFS}/{month['id']}")

    assert response.status_code == 204


@pytest.mark.usefixtures("owner")
async def test_missing_tariff_is_404(shop: Shop) -> None:
    assert (await shop.http.put(f"{TARIFFS}/999999", json=_body())).status_code == 404
    assert (await shop.http.post(f"{TARIFFS}/999999/archive", json={})).status_code == 404
    assert (await shop.http.delete(f"{TARIFFS}/999999")).status_code == 404


# --- 2.10: без перезапуска ---


@pytest.mark.usefixtures("owner")
async def test_2_10_changes_apply_without_restart(shop: Shop) -> None:
    """2.10: тариф, созданный в админке, сразу в продаже и закрывает пункт чек-листа."""

    async def tariffs_item() -> str:
        items = (await shop.http.get("/api/admin/checklist")).json()["items"]
        status: str = next(item["status"] for item in items if item["key"] == "tariffs")
        return status

    assert await tariffs_item() == "todo"

    created = await _create(shop, name="Месяц")
    assert await _names_on_sale(shop.session) == ["Месяц"]
    assert await tariffs_item() == "done"

    await shop.http.put(f"{TARIFFS}/{created['id']}", json=_body(name="Месяц VPN"))
    assert await _names_on_sale(shop.session) == ["Месяц VPN"]


# --- Права ---


async def test_tariffs_are_owner_only(shop: Shop) -> None:
    """01-domain: настройки тарифов — только владелец."""
    helper = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, helper)
    await shop.sign_in(helper)

    assert (await shop.http.get(TARIFFS)).status_code == 403
    assert (await shop.http.post(TARIFFS, json=_body())).status_code == 403
    assert (await shop.http.get(SQUADS)).status_code == 403


async def test_tariffs_need_sign_in(shop: Shop) -> None:
    assert (await shop.http.get(TARIFFS)).status_code == 401
