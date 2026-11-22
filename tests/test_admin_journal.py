"""Экран «Журнал» в API админки (А10; 4.35, 4.26, 4.9).

Журнал не очищается между тестами (4.26): тесты с настоящими коммитами оставляют в
нём записи. Поэтому проверки смотрят на записи с действиями этого файла (группа
`rbtest`) или на самые новые записи.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Actor, ActorType, JournalEntry, JsonValue, Outcome, Subject, record
from remnabay.panel import SIGNATURE_HEADER
from remnabay.shop_settings import SHOP_TIME_ZONE, set_setting
from remnabay.web.admin import openapi_schema
from remnabay.web.admin._journal import PAGE_SIZE
from remnabay.web.panel_webhook import PANEL_WEBHOOK_PATH
from tests.domain_support import add, make_client, make_payment, make_subscription, make_tariff
from tests.web_support import Shop, running_shop

API = "/api/admin"
JOURNAL = f"{API}/journal"


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    owner = TeamMember(telegram_id=1, role=TeamRole.OWNER, name="Анна")
    await add(shop.session, owner)
    await shop.sign_in(owner)
    return owner


async def _record(
    shop: Shop,
    action: str,
    *,
    actor: Actor = Actor.SYSTEM,
    outcome: Outcome = Outcome.SUCCESS,
    subject: Subject | None = None,
    details: dict[str, JsonValue] | None = None,
) -> JournalEntry:
    return await record(
        shop.session,
        actor=actor,
        action=action,
        outcome=outcome,
        subject=subject,
        details=details,
    )


async def _get(shop: Shop, **query: Any) -> dict[str, Any]:
    response = await shop.http.get(JOURNAL, params=query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _actions(body: dict[str, Any]) -> list[str]:
    return [entry["action"] for entry in body["entries"]]


# --- Порядок и постраничность ---


@pytest.mark.usefixtures("owner")
async def test_4_35_newest_first_with_pages(shop: Shop) -> None:
    """4.35: записи журнала — новые сверху; следующая страница — по `next_before`, без
    пропусков и повторов; на последней странице `next_before` пуст."""
    total = PAGE_SIZE + 3
    for number in range(total):
        await _record(shop, f"rbtest.page_{number}")

    first = await _get(shop, action_group="rbtest")
    assert len(first["entries"]) == PAGE_SIZE
    assert first["entries"][0]["action"] == f"rbtest.page_{total - 1}"
    assert first["next_before"] == first["entries"][-1]["id"]

    second = await _get(shop, action_group="rbtest", before=first["next_before"])
    assert second["next_before"] is None
    seen = _actions(first) + _actions(second)
    assert seen == [f"rbtest.page_{number}" for number in reversed(range(total))]


@pytest.mark.usefixtures("owner")
async def test_4_35_order_by_time_of_action(shop: Shop) -> None:
    """4.35: «новые сверху» — по времени действия, даже если запись о более раннем
    действии добавлена позже."""
    late = JournalEntry(
        actor_type=ActorType.SYSTEM,
        action="rbtest.late",
        outcome=Outcome.SUCCESS,
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    early = JournalEntry(
        actor_type=ActorType.SYSTEM,
        action="rbtest.early",
        outcome=Outcome.SUCCESS,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await add(shop.session, late, early)

    body = await _get(shop, action_group="rbtest")
    assert _actions(body) == ["rbtest.late", "rbtest.early"]


@pytest.mark.usefixtures("owner")
async def test_4_35_unknown_cursor_is_not_found(shop: Shop) -> None:
    """4.35: несуществующая позиция страницы — 404, а не пустой или чужой список."""
    response = await shop.http.get(JOURNAL, params={"before": 10**15})
    assert response.status_code == 404


# --- Фильтры ---


@pytest.mark.usefixtures("owner")
async def test_4_35_filter_by_actor_type(shop: Shop, owner: TeamMember) -> None:
    """4.35: фильтр «кто» — участник команды, клиент, система, панель, провайдер."""
    client = await make_client(shop.session, telegram_id=555)
    await _record(shop, "rbtest.by_member", actor=Actor.team_member(owner.id))
    await _record(shop, "rbtest.by_client", actor=Actor.client(client.id))
    await _record(shop, "rbtest.by_system", actor=Actor.SYSTEM)
    await _record(shop, "rbtest.by_panel", actor=Actor.PANEL)
    await _record(shop, "rbtest.by_provider", actor=Actor.provider("yookassa"))

    expected = {
        "team_member": "rbtest.by_member",
        "client": "rbtest.by_client",
        "system": "rbtest.by_system",
        "panel": "rbtest.by_panel",
        "provider": "rbtest.by_provider",
    }
    for actor_type, action in expected.items():
        body = await _get(shop, action_group="rbtest", actor_type=actor_type)
        assert _actions(body) == [action], actor_type

    by_member = (await _get(shop, action="rbtest.by_member"))["entries"][0]
    by_client = (await _get(shop, action="rbtest.by_client"))["entries"][0]
    # Кто — по имени, как в карточке клиента (10.7)
    assert by_member["actor"] == "Анна"
    assert by_client["actor_client"]["telegram_id"] == 555


@pytest.mark.usefixtures("owner")
async def test_4_35_filter_by_action_and_group(shop: Shop) -> None:
    """4.35: фильтр «что» — одно действие или вся группа действий (`payment` — все
    `payment.*`); подчёркивание в группе не работает как шаблон LIKE."""
    await _record(shop, "rbtest.one")
    await _record(shop, "rbtest.two")
    await _record(shop, "rbtestx.other")

    assert _actions(await _get(shop, action="rbtest.one")) == ["rbtest.one"]
    assert _actions(await _get(shop, action_group="rbtest")) == ["rbtest.two", "rbtest.one"]
    # `rb_test` не должен совпасть с `rbXtest` и наоборот: `_` — обычный символ
    await _record(shop, "rb_test.exact")
    await _record(shop, "rbxtest.not_matched")
    assert _actions(await _get(shop, action_group="rb_test")) == ["rb_test.exact"]


@pytest.mark.usefixtures("owner")
async def test_4_35_filter_by_subject(shop: Shop) -> None:
    """4.35: фильтр «над чем» — вид объекта, при желании id; `none` — записи без
    объекта. Для подписки и платежа виден клиент — для ссылки на его карточку."""
    client = await make_client(shop.session, telegram_id=777)
    tariff = make_tariff()
    await add(shop.session, tariff)
    subscription = make_subscription(client, tariff, panel_user_id=7)
    await add(shop.session, subscription)
    payment = make_payment(client, tariff)
    other = make_payment(client, tariff)
    await add(shop.session, payment, other)
    await _record(shop, "rbtest.sub", subject=Subject("subscription", subscription.id))
    await _record(shop, "rbtest.pay", subject=Subject("payment", payment.id))
    await _record(shop, "rbtest.pay_other", subject=Subject("payment", other.id))
    await _record(shop, "rbtest.tariff", subject=Subject("tariff", tariff.id))
    await _record(shop, "rbtest.nothing")

    payments = await _get(shop, action_group="rbtest", subject_type="payment")
    assert _actions(payments) == ["rbtest.pay_other", "rbtest.pay"]
    one = await _get(shop, action_group="rbtest", subject_type="payment", subject_id=payment.id)
    assert _actions(one) == ["rbtest.pay"]
    assert one["entries"][0]["subject"]["client"]["telegram_id"] == 777

    [sub] = (await _get(shop, action_group="rbtest", subject_type="subscription"))["entries"]
    assert sub["subject"] == {
        "type": "subscription",
        "id": str(subscription.id),
        "client": {"id": client.id, "telegram_id": 777, "username": None, "name": None},
        "name": "Основная",
    }
    [tariff_row] = (await _get(shop, action_group="rbtest", subject_type="tariff"))["entries"]
    assert tariff_row["subject"]["name"] == "Месяц"

    nothing = await _get(shop, action_group="rbtest", subject_type="none")
    assert _actions(nothing) == ["rbtest.nothing"]
    assert nothing["entries"][0]["subject"] is None


@pytest.mark.usefixtures("owner")
async def test_4_35_filter_by_outcome(shop: Shop) -> None:
    """4.35: фильтр по результату — успех или ошибка."""
    await _record(shop, "rbtest.ok")
    await _record(shop, "rbtest.failed", outcome=Outcome.FAILURE)

    assert _actions(await _get(shop, action_group="rbtest", outcome="failure")) == ["rbtest.failed"]
    assert _actions(await _get(shop, action_group="rbtest", outcome="success")) == ["rbtest.ok"]


async def test_4_35_period_in_shop_time_zone(shop: Shop, owner: TeamMember) -> None:
    """4.35, 1.18: период — даты в часовом поясе магазина, конец включительно."""
    await set_setting(shop.session, SHOP_TIME_ZONE, "Asia/Vladivostok", member_id=owner.id)
    # 10 января 20:30 UTC — уже 11 января во Владивостоке (UTC+10)
    entry = JournalEntry(
        actor_type=ActorType.SYSTEM,
        action="rbtest.night",
        outcome=Outcome.SUCCESS,
        occurred_at=datetime(2026, 1, 10, 20, 30, tzinfo=UTC),
    )
    await add(shop.session, entry)

    on_day = await _get(shop, action_group="rbtest", since="2026-01-11", until="2026-01-11")
    assert _actions(on_day) == ["rbtest.night"]
    assert on_day["time_zone"] == "Asia/Vladivostok"
    before_day = await _get(shop, action_group="rbtest", until="2026-01-10")
    assert _actions(before_day) == []
    after_day = await _get(shop, action_group="rbtest", since="2026-01-12")
    assert _actions(after_day) == []


@pytest.mark.usefixtures("owner")
async def test_4_35_period_like_home_screen(shop: Shop) -> None:
    """4.35, 1.18: периоды «сегодня», «7 дней», «30 дней» — как на главной, от полуночи
    по времени магазина."""
    now = datetime.now(UTC)
    for action, days_ago in (("rbtest.now", 0), ("rbtest.days_3", 3), ("rbtest.days_10", 10)):
        await add(
            shop.session,
            JournalEntry(
                actor_type=ActorType.SYSTEM,
                action=action,
                outcome=Outcome.SUCCESS,
                occurred_at=now - timedelta(days=days_ago),
            ),
        )

    assert _actions(await _get(shop, action_group="rbtest", period="today")) == ["rbtest.now"]
    assert _actions(await _get(shop, action_group="rbtest", period="7d")) == [
        "rbtest.now",
        "rbtest.days_3",
    ]
    assert len((await _get(shop, action_group="rbtest", period="30d"))["entries"]) == 3


@pytest.mark.usefixtures("owner")
async def test_4_35_filters_reject_garbage(shop: Shop) -> None:
    """4.35: неизвестный вид действующего лица или результат — ошибка запроса (Pydantic)."""
    for query in ({"actor_type": "robot"}, {"outcome": "maybe"}, {"action": "drop table"}):
        response = await shop.http.get(JOURNAL, params=query)
        assert response.status_code == 422, query


# --- Записи без клиента и путь «вебхук или сверка» ---


@pytest.mark.usefixtures("owner")
async def test_4_35_rejected_panel_webhook_is_visible(shop: Shop) -> None:
    """4.35: отклонённый вебхук панели — запись без клиента и без объекта — видна в
    журнале с причиной."""
    response = await shop.http.post(
        PANEL_WEBHOOK_PATH,
        content=b"{}",
        headers={"content-type": "application/json", SIGNATURE_HEADER: "bad"},
    )
    assert response.status_code == 401

    body = await _get(shop, action="panel.webhook_rejected", outcome="failure")
    newest = body["entries"][0]
    assert newest["actor_type"] == "system"
    assert newest["subject"] is None
    assert newest["details"]["reason"] == "signature"


@pytest.mark.usefixtures("owner")
async def test_4_35_entries_without_client_are_listed(shop: Shop) -> None:
    """4.35: рассылки, «Применить в Telegram», миграция и отклонённое уведомление
    провайдера видны в общем списке, хотя к клиенту не привязаны."""
    await _record(shop, "broadcast.finished", subject=Subject("broadcast", 1))
    await _record(shop, "brand.applied_to_telegram", subject=Subject("telegram_bot", "profile"))
    await _record(shop, "migration.applied", subject=Subject("migration_run", 1))
    await _record(
        shop,
        "payment.notification_rejected",
        outcome=Outcome.FAILURE,
        subject=Subject("payment_provider", "yookassa"),
        details={"reason": "signature", "provider": "yookassa"},
    )

    newest = (await _get(shop))["entries"][:4]
    assert [entry["action"] for entry in newest] == [
        "payment.notification_rejected",
        "migration.applied",
        "brand.applied_to_telegram",
        "broadcast.finished",
    ]
    assert all(entry["subject"]["client"] is None for entry in newest)


@pytest.mark.usefixtures("owner")
async def test_4_9_source_of_panel_change_is_shown(shop: Shop) -> None:
    """4.9: в журнале видно, каким путём магазин узнал об изменении — вебхук или сверка."""
    client = await make_client(shop.session, telegram_id=888)
    subscription = make_subscription(client, None, panel_user_id=8)
    await add(shop.session, subscription)
    subject = Subject("subscription", subscription.id)
    await _record(
        shop,
        "subscription.changed_in_panel",
        actor=Actor.PANEL,
        subject=subject,
        details={"source": "webhook", "event": "user.modified"},
    )
    await _record(
        shop,
        "subscription.changed_in_panel",
        actor=Actor.PANEL,
        subject=subject,
        details={"source": "sync", "event": "sync"},
    )

    body = await _get(
        shop,
        action="subscription.changed_in_panel",
        subject_type="subscription",
        subject_id=subscription.id,
    )
    assert [entry["details"]["source"] for entry in body["entries"]] == ["sync", "webhook"]
    assert body["entries"][0]["subject"]["client"]["telegram_id"] == 888


# --- Только чтение и права ---


@pytest.mark.usefixtures("owner")
async def test_4_26_journal_is_read_only(shop: Shop) -> None:
    """4.26: через API админки запись журнала нельзя изменить или удалить: у раздела
    «Журнал» есть только чтение."""
    entry = await _record(shop, "rbtest.keep")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in (JOURNAL, f"{JOURNAL}/{entry.id}"):
            response = await shop.http.request(method, path)
            assert response.status_code in {404, 405}, (method, path)

    journal_paths = {
        path: set(methods)
        for path, methods in openapi_schema()["paths"].items()
        if path.startswith(JOURNAL)
    }
    assert journal_paths == {JOURNAL: {"get"}}
    assert _actions(await _get(shop, action="rbtest.keep")) == ["rbtest.keep"]


async def test_4_35_assistant_sees_journal(shop: Shop) -> None:
    """4.35: журнал смотрит и помощник — в таблице ролей (01-domain) он не закрыт."""
    assistant = TeamMember(telegram_id=2, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)
    await _record(shop, "rbtest.seen")

    assert _actions(await _get(shop, action="rbtest.seen")) == ["rbtest.seen"]


async def test_4_35_journal_needs_sign_in(shop: Shop) -> None:
    """4.35: без входа журнал не отдаётся."""
    response = await shop.http.get(JOURNAL)
    assert response.status_code == 401
