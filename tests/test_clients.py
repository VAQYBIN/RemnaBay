"""Поиск клиента, карточка и действия команды (А2, А3; 10.1–10.7).

Также 4.15, 4.32 для выдачи дней и 4.12 (данные при недоступной панели).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.client_actions import add_granted_days
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import PaymentState, RefundRequest, RefundRequestState
from remnabay.domain.referrals import ReferralLink
from remnabay.domain.subscriptions import PanelUserStatus, SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import TariffType
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.domain.trials import Trial
from remnabay.journal import Actor, ActorType, Subject, entries_for
from remnabay.messaging import SendArgs, send_message
from remnabay.panel import PanelUnavailableError
from remnabay.panel_sync import PanelOutage
from remnabay.queue import (
    AttemptResult,
    QueueAttempt,
    TaskContext,
    TaskStatus,
    cancel_failed,
)
from remnabay.queue._models import QueueTask
from remnabay.worker.main import TASKS
from tests.domain_support import add, make_client, make_payment, make_subscription, make_tariff
from tests.guide_support import queued, run_queued
from tests.panel_support import device_json, fake_runtime
from tests.web_support import Shop, running_shop

API = "/api/admin"
DAY = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    owner = TeamMember(telegram_id=1, role=TeamRole.OWNER, name="Владелец")
    await add(shop.session, owner)
    return owner


@pytest.fixture
async def assistant(shop: Shop, owner: TeamMember) -> TeamMember:
    """Искать клиента и действовать в карточке может и помощник (01-domain)."""
    del owner
    assistant = TeamMember(telegram_id=2, role=TeamRole.ASSISTANT, name="Помощник")
    await add(shop.session, assistant)
    await shop.sign_in(assistant)
    return assistant


async def _client(
    shop: Shop, telegram_id: int, *, username: str | None = None, first_name: str | None = None
) -> Client:
    client = await make_client(shop.session, telegram_id=telegram_id)
    account = await shop.session.get(TelegramAccount, telegram_id)
    assert account is not None
    account.username = username
    account.first_name = first_name
    await shop.session.flush()
    return client


async def _subscription(
    shop: Shop,
    client: Client,
    *,
    user_id: int = 7,
    ends_in: timedelta = 10 * DAY,
    status: PanelUserStatus = PanelUserStatus.ACTIVE,
    tariff_type: TariffType = TariffType.TERM_UNLIMITED,
) -> Subscription:
    """Подписка с отрезком срока и тот же срок в подставной панели."""
    extra: dict[str, Any] = (
        {"duration_days": None, "traffic_limit_bytes": 10 * 1024**3}
        if tariff_type == TariffType.PACKAGE_ONLY
        else {}
    )
    tariff = make_tariff(name="Месяц", type=tariff_type, **extra)
    await add(shop.session, tariff)
    subscription = make_subscription(client, tariff, panel_user_id=user_id)
    end = _now() + ends_in
    subscription.expires_at = end
    subscription.panel_status = status
    await add(shop.session, subscription)
    await add(
        shop.session,
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.GRANTED,
            starts_at=end - 30 * DAY,
            ends_at=end,
        ),
    )
    shop.panel.add_user(
        id=user_id,
        shortUuid=subscription.panel_short_uuid,
        username=subscription.panel_username,
        subscriptionUrl=subscription.subscription_url,
        expireAt=end.isoformat(),
        status=status.value.upper(),
    )
    return subscription


async def _messages(session: AsyncSession) -> list[SendArgs]:
    return [SendArgs.model_validate(task.args) for task in await queued(session, send_message)]


# --- Поиск (10.1) ---


@pytest.mark.usefixtures("assistant")
async def test_10_1_search_by_id_username_name_link_and_panel_username(shop: Shop) -> None:
    """10.1: клиента находят по Telegram ID, username, имени, ссылке подписки или имени
    пользователя в панели."""
    anna = await _client(shop, 111222, username="anna_vpn", first_name="Анна")
    boris = await _client(shop, 333444, username="boris", first_name="Борис")
    subscription = await _subscription(shop, boris, user_id=9)

    async def found(query: str) -> list[int]:
        body = (await shop.http.get(f"{API}/clients", params={"q": query})).json()
        return [row["client"]["id"] for row in body["clients"]]

    assert await found("111222") == [anna.id]
    assert await found("@ANNA_vpn") == [anna.id]
    assert await found("анна") == [anna.id]
    assert await found(subscription.subscription_url) == [boris.id]
    assert await found(subscription.panel_short_uuid) == [boris.id]
    assert await found(subscription.panel_username) == [boris.id]
    assert await found("нет такого") == []
    # Знаки шаблона LIKE ищутся буквально
    assert await found("%") == []
    assert set(await found("")) >= {anna.id, boris.id}


@pytest.mark.usefixtures("assistant")
async def test_a2_list_row_shows_subscriptions_nearest_end_and_flags(shop: Shop) -> None:
    """А2: число подписок, ближайшая дата окончания, пометки «бот заблокирован» и
    «усыновлён»."""
    client = await _client(shop, 555, username="c")
    client.bot_blocked_at = _now()
    client.adopted_at = _now()
    await _subscription(shop, client, user_id=7, ends_in=10 * DAY)
    nearest = await _subscription(shop, client, user_id=8, ends_in=3 * DAY)

    [row] = (await shop.http.get(f"{API}/clients", params={"q": "555"})).json()["clients"]

    assert row["subscriptions"] == 2
    assert datetime.fromisoformat(row["nearest_end"]) == nearest.expires_at
    assert (row["bot_blocked"], row["adopted"]) == (True, True)


# --- Карточка (10.2) ---


@pytest.mark.usefixtures("assistant")
async def test_10_2_card_shows_subscriptions_payments_trial_referrals_and_journal(
    shop: Shop,
) -> None:
    """10.2: подписки с состоянием из панели (тариф, срок, статус), платежи, триал,
    реферальные связи, пометки и журнал действий по клиенту."""
    client = await _client(shop, 777, username="client", first_name="Клиент")
    client.adopted_at = _now()
    subscription = await _subscription(shop, client, status=PanelUserStatus.DISABLED)
    payment = make_payment(
        client, None, state=PaymentState.APPLIED, subscription_id=subscription.id
    )
    await add(shop.session, payment)
    await add(shop.session, Trial(client_id=client.id, subscription_id=subscription.id))
    inviter = await _client(shop, 100, first_name="Пригласивший")
    invited = await _client(shop, 200, first_name="Приглашённый")
    await add(
        shop.session,
        ReferralLink(invited_id=client.id, inviter_id=inviter.id),
        ReferralLink(invited_id=invited.id, inviter_id=client.id),
    )
    deleted = await _subscription(shop, client, user_id=8)
    deleted.deleted_at = _now()

    card = (await shop.http.get(f"{API}/clients/{client.id}")).json()

    assert card["client"]["telegram_id"] == 777
    assert (card["adopted"], card["bot_blocked"]) == (True, False)
    live, removed = card["subscriptions"]
    assert (live["id"], live["tariff_name"], live["status"], live["deleted"]) == (
        subscription.id,
        "Месяц",
        "disabled",
        False,
    )
    assert datetime.fromisoformat(live["expires_at"]) == subscription.expires_at
    # Дни можно выдать и отключённой в панели подписке (решение 0065)
    assert live["can_grant"] is True
    assert (removed["id"], removed["deleted"], removed["can_grant"]) == (deleted.id, True, False)
    assert [row["id"] for row in card["payments"]] == [payment.id]
    assert card["trial"]["subscription_id"] == subscription.id
    assert card["inviter"]["id"] == inviter.id
    assert [ref["id"] for ref in card["invited"]] == [invited.id]


async def test_10_2_card_devices_come_from_panel(shop: Shop, assistant: TeamMember) -> None:
    """10.2: устройства подписки — из панели при открытии (6.2)."""
    del assistant
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    shop.panel.devices[7] = [device_json(hwid="h1", userAgent="Happ/2.6.0/ios")]

    url = f"{API}/clients/{client.id}/subscriptions/{subscription.id}/devices"
    body = (await shop.http.get(url)).json()

    assert body["enabled"] is True
    assert [(d["hwid"], d["app"]) for d in body["devices"]] == [("h1", "Happ")]
    shop.panel.down = True
    assert (await shop.http.get(url)).status_code == 503


# --- Выдать дни (10.3, 10.4) ---


async def _grant(shop: Shop, client: Client, subscription: Subscription, **body: Any) -> Any:
    return await shop.http.post(
        f"{API}/clients/{client.id}/subscriptions/{subscription.id}/grant-days",
        json={"days": 7, "comment": "Компенсация за простой", **body},
    )


async def _run_grants(shop: Shop) -> int:
    with fake_runtime(panel=shop.panel):
        return await run_queued(shop.session, add_granted_days)


async def test_10_3_10_4_grant_days_to_active_subscription(
    shop: Shop, assistant: TeamMember
) -> None:
    """10.3, 10.4, 10.7: дни добавляются в панели и отрезком нулевой стоимости; клиент
    получает «Добавлены дни» после применения; комментарий — только в журнале."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    old_end = subscription.expires_at
    assert old_end is not None

    response = await _grant(shop, client, subscription)
    assert response.status_code == 200
    assert response.json()["subscriptions"][0]["changes_waiting"] == 1
    assert await _messages(shop.session) == []

    assert await _run_grants(shop) == 1

    assert subscription.expires_at == old_end + 7 * DAY
    segment = await shop.session.scalar(
        select(TermSegment).where(TermSegment.ends_at == old_end + 7 * DAY)
    )
    assert segment is not None
    assert (segment.kind, segment.cost, segment.starts_at) == (SegmentKind.GRANTED, 0, old_end)
    [message] = await _messages(shop.session)
    assert message.text_key == "event.days_added"
    assert message.variables["days_text"] == "7 дней"
    assert message.dates["end_date"] == old_end + 7 * DAY
    assert "Компенсация" not in str(message.model_dump())
    [granted] = [
        e
        for e in await entries_for(shop.session, Subject("subscription", subscription.id))
        if e.action == "subscription.days_granted"
    ]
    assert (granted.actor_type, granted.actor_ref) == (ActorType.TEAM_MEMBER, str(assistant.id))
    assert granted.details["comment"] == "Компенсация за простой"
    card = (await shop.http.get(f"{API}/clients/{client.id}")).json()
    assert {"subscription.days_granted", "subscription.days_added"} <= {
        e["action"] for e in card["journal"]
    }
    assert "Помощник" in {e["actor"] for e in card["journal"]}


@pytest.mark.usefixtures("assistant")
async def test_10_3_expired_subscription_counts_from_grant(shop: Shop) -> None:
    """10.3: если подписка истекла, дни отсчитываются от момента выдачи."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client, ends_in=-5 * DAY)
    before = _now()

    await _grant(shop, client, subscription, days=3)
    await _run_grants(shop)

    assert subscription.expires_at is not None
    assert before + 3 * DAY <= subscription.expires_at <= datetime.now(UTC) + 3 * DAY


@pytest.mark.usefixtures("assistant")
async def test_10_3_limits_comment_and_term_required(shop: Shop) -> None:
    """10.3: от 1 до 3650 дней, комментарий обязателен, только подписке со сроком."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    package = await _subscription(shop, client, user_id=8, tariff_type=TariffType.PACKAGE_ONLY)
    other = await _subscription(shop, await _client(shop, 888), user_id=9)

    assert (await _grant(shop, client, subscription, days=0)).status_code == 422
    assert (await _grant(shop, client, subscription, days=3651)).status_code == 422
    assert (await _grant(shop, client, subscription, comment="")).status_code == 422
    assert (await _grant(shop, client, subscription, comment="   ")).status_code == 409
    assert (await _grant(shop, client, package)).status_code == 409
    assert (await _grant(shop, client, other)).status_code == 409
    assert (await _grant(shop, client, subscription, days=3650)).status_code == 200


@pytest.mark.usefixtures("assistant")
async def test_4_15_granted_days_are_not_added_twice_after_lost_answer(shop: Shop) -> None:
    """4.15: прошлая попытка добавила дни в панели, а ответ потерялся — повтор не
    добавляет их второй раз."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    old_end = subscription.expires_at
    assert old_end is not None
    await _grant(shop, client, subscription)
    [task] = await queued(shop.session, add_granted_days)

    shop.panel.lose_next_write = True
    context = TaskContext(session=shop.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=shop.panel), pytest.raises(PanelUnavailableError):
        async with shop.session.begin_nested():
            await add_granted_days.run(context, task.args)
    await _run_grants(shop)

    assert [name for name, _ in shop.panel.writes].count("PATCH /api/users") == 1
    assert subscription.expires_at == old_end + 7 * DAY


@pytest.mark.usefixtures("assistant")
async def test_4_32_cancelled_grant_tells_client_action_failed(
    shop: Shop, owner: TeamMember
) -> None:
    """4.31, 4.32: проваленную выдачу дней можно отменить — дни не добавлены, клиент
    получает «Не удалось выполнить действие»."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    await _grant(shop, client, subscription)
    [task] = await queued(shop.session, add_granted_days)
    task.status = TaskStatus.FAILED
    await add(
        shop.session,
        QueueAttempt(task_id=task.id, number=1, retry_round=1, result=AttemptResult.ERROR),
    )
    await cancel_failed(
        shop.session, TASKS, task.id, actor=Actor.team_member(owner.id), comment="Не нужно"
    )

    [message] = await _messages(shop.session)
    assert message.text_key == "event.action_failed"
    assert (
        await shop.session.scalar(
            select(TermSegment.id).where(TermSegment.subscription_id == subscription.id).offset(1)
        )
        is None
    )


# --- Сбросить устройства, синхронизировать (10.5) ---


async def test_10_5_reset_devices_by_team(shop: Shop, assistant: TeamMember) -> None:
    """10.5: участник команды сбрасывает устройства подписки — все удаляются в панели,
    действие — в журнале с участником; панель недоступна — удаление в очереди."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    shop.panel.devices[7] = [device_json(hwid="h1"), device_json(hwid="h2")]
    url = f"{API}/clients/{client.id}/subscriptions/{subscription.id}/reset-devices"

    response = await shop.http.post(url)

    assert response.json()["result"] == "done"
    assert shop.panel.devices[7] == []
    [entry] = [
        e
        for e in await entries_for(shop.session, Subject("subscription", subscription.id))
        if e.action == "device.deleted_all"
    ]
    assert (entry.actor_type, entry.actor_ref) == (ActorType.TEAM_MEMBER, str(assistant.id))

    shop.panel.down = True
    await add(shop.session, PanelOutage(started_at=_now()))
    assert (await shop.http.post(url)).json()["result"] == "queued"


@pytest.mark.usefixtures("assistant")
async def test_10_5_sync_with_panel_now(shop: Shop) -> None:
    """10.5: «Синхронизировать с панелью» сразу принимает состояние панели (4.3–4.7);
    панель недоступна — понятный отказ."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    new_end = _now() + 40 * DAY
    shop.panel.users[7]["expireAt"] = new_end.isoformat()
    shop.panel.users[7]["status"] = "DISABLED"
    url = f"{API}/clients/{client.id}/subscriptions/{subscription.id}/sync"

    card = (await shop.http.post(url)).json()

    [row] = card["subscriptions"]
    assert datetime.fromisoformat(row["expires_at"]) == new_end
    assert row["status"] == "disabled"
    actions = [e["action"] for e in card["journal"]]
    assert "subscription.sync_requested" in actions
    assert "subscription.changed_in_panel" in actions

    shop.panel.down = True
    assert (await shop.http.post(url)).status_code == 503


# --- Запросы на возврат (10.6) ---


async def test_10_6_assistant_files_request_only_owner_declines(
    shop: Shop, assistant: TeamMember, owner: TeamMember
) -> None:
    """10.6: помощник фиксирует запрос с комментарием, отказать может только владелец;
    клиент получает уведомление об отказе."""
    del assistant
    client = await _client(shop, 777)
    base = f"{API}/clients/{client.id}/refund-requests"

    card = (await shop.http.post(base, json={"comment": "Не подключается"})).json()
    [request] = card["refund_requests"]
    assert (request["state"], request["comment"], request["created_by"]) == (
        "open",
        "Не подключается",
        "Помощник",
    )
    decline = f"{base}/{request['id']}/decline"
    assert (await shop.http.post(decline, json={"comment": "Нет"})).status_code == 403

    await shop.sign_in(owner)
    card = (await shop.http.post(decline, json={"comment": "Помогли настроить"})).json()

    [closed] = card["refund_requests"]
    assert (closed["state"], closed["closed_by"], closed["close_comment"]) == (
        "rejected",
        "Владелец",
        "Помогли настроить",
    )
    [message] = await _messages(shop.session)
    assert message.text_key == "event.refund_declined"
    stored = await shop.session.get(RefundRequest, request["id"])
    assert stored is not None
    assert stored.state == RefundRequestState.REJECTED
    assert {"refund_request.filed", "refund_request.declined"} <= {
        e["action"] for e in card["journal"]
    }


async def test_10_7_unknown_client_and_foreign_subscription_are_rejected(
    shop: Shop, assistant: TeamMember
) -> None:
    del assistant
    assert (await shop.http.get(f"{API}/clients/999999")).status_code == 404
    response = await shop.http.post(f"{API}/clients/999999/refund-requests", json={"comment": "x"})
    assert response.status_code == 409


# --- Порядок операций подписки (4.17) ---


@pytest.mark.usefixtures("assistant")
async def test_4_17_grant_waits_behind_failed_operation(shop: Shop) -> None:
    """4.17, 4.27: выдача дней — операция подписки; за проваленной операцией она ждёт."""
    client = await _client(shop, 777)
    subscription = await _subscription(shop, client)
    key = f"subscription:{subscription.id}"
    await add(
        shop.session, QueueTask(name="devices.delete", args={}, key=key, status=TaskStatus.FAILED)
    )

    await _grant(shop, client, subscription)

    [task] = await queued(shop.session, add_granted_days)
    assert task.key == key
