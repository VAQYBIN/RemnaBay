"""Сверка подписки с панелью: панель всегда права (4.3–4.10, 4.29)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentState
from remnabay.domain.subscriptions import (
    PanelUserStatus,
    SegmentKind,
    Subscription,
    TermSegment,
)
from remnabay.domain.tariffs import TrafficResetStrategy
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Actor, JournalEntry, Subject, entries_for
from remnabay.messaging import SendArgs
from remnabay.panel import PanelUnavailableError
from remnabay.panel_sync import (
    ReconcileArgs,
    Source,
    SyncPageArgs,
    current_outage,
    enqueue_reconcile,
    health_check,
    panel_available,
    reconcile,
    sync_interval,
    sync_page,
)
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask
from remnabay.shop_settings import PANEL_OUTAGE_ALERT_AFTER, PANEL_SYNC_INTERVAL, set_setting
from tests.domain_support import (
    add,
    make_client,
    make_payment,
    make_subscription,
    make_team_member,
)
from tests.panel_support import FakePanel, fake_runtime, user_json

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
WEBHOOK = ReconcileArgs(subscription_id=0, source=Source.WEBHOOK, event="user.modified")


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", ".000Z")


async def _subscription(session: AsyncSession) -> tuple[Client, Subscription, Payment]:
    """Подписка, оплаченная на месяц T0–T1; в панели — то же самое."""
    client = await make_client(session)
    payment = make_payment(client, None, state=PaymentState.APPLIED)
    await add(session, payment)
    subscription = make_subscription(client, None, panel_user_id=7)
    subscription.panel_short_uuid = "abc123"
    subscription.subscription_url = "https://sub.example.com/abc123"
    subscription.expires_at = T1
    subscription.device_limit = 3
    subscription.traffic_limit_bytes = 0
    subscription.panel_status = PanelUserStatus.ACTIVE
    await add(session, subscription)
    await add(
        session,
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.PAYMENT,
            payment_id=payment.id,
            starts_at=T0,
            ends_at=T1,
            cost=Decimal("199.00"),
        ),
    )
    return client, subscription, payment


async def _reconcile(
    session: AsyncSession,
    subscription: Subscription,
    panel: FakePanel,
    args: ReconcileArgs = WEBHOOK,
) -> None:
    with fake_runtime(panel=panel):
        await reconcile(session, subscription, args)
    await session.flush()


async def _segments(session: AsyncSession, subscription: Subscription) -> list[tuple[object, ...]]:
    rows = await session.scalars(
        select(TermSegment)
        .where(TermSegment.subscription_id == subscription.id)
        .order_by(TermSegment.starts_at)
    )
    return [(s.kind, s.starts_at, s.ends_at, s.cost) for s in rows]


async def _journal(
    session: AsyncSession, subscription: Subscription
) -> list[tuple[str, dict[str, object]]]:
    entries = await entries_for(session, Subject("subscription", subscription.id))
    assert all(entry.actor == Actor.PANEL for entry in entries)
    return [(entry.action, dict(entry.details)) for entry in entries]


async def test_4_3_term_extended_in_panel_adds_free_segment(db_session: AsyncSession) -> None:
    """4.3, 4.9: дату окончания увеличили в панели — отрезок нулевой стоимости; в журнале —
    что изменилось и что узнали из вебхука."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel({7: user_json(expireAt=_iso(T2))})

    await _reconcile(db_session, subscription, panel)

    assert await _segments(db_session, subscription) == [
        (SegmentKind.PAYMENT, T0, T1, Decimal("199.00")),
        (SegmentKind.PANEL, T1, T2, Decimal("0.00")),
    ]
    assert subscription.expires_at == T2
    assert await _journal(db_session, subscription) == [
        (
            "subscription.changed_in_panel",
            {
                "source": "webhook",
                "event": "user.modified",
                "changes": {"expires_at": [T1.isoformat(), T2.isoformat()]},
            },
        )
    ]


async def test_4_4_term_shortened_in_panel_cuts_segments_from_end(
    db_session: AsyncSession,
) -> None:
    """4.4: дату окончания уменьшили в панели — отрезки срезаются с конца."""
    _client, subscription, _payment = await _subscription(db_session)
    await add(
        db_session,
        TermSegment(
            subscription_id=subscription.id, kind=SegmentKind.GRANTED, starts_at=T1, ends_at=T2
        ),
    )
    middle = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    panel = FakePanel({7: user_json(expireAt=_iso(middle))})

    await _reconcile(db_session, subscription, panel)

    assert await _segments(db_session, subscription) == [
        (SegmentKind.PAYMENT, T0, middle, Decimal("199.00"))
    ]


async def test_4_5_limits_changed_in_panel_are_taken_as_fact(db_session: AsyncSession) -> None:
    """4.5: лимиты и стратегию сброса из панели магазин принимает как факт."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel(
        {
            7: user_json(
                expireAt=_iso(T1),
                hwidDeviceLimit=5,
                trafficLimitBytes=10 * 1024**3,
                trafficLimitStrategy="MONTH_ROLLING",
            )
        }
    )

    await _reconcile(db_session, subscription, panel)

    assert subscription.device_limit == 5
    assert subscription.traffic_limit_bytes == 10 * 1024**3
    assert subscription.traffic_reset_strategy == TrafficResetStrategy.MONTH_FROM_CREATION
    [(_action, details)] = await _journal(db_session, subscription)
    assert details["changes"] == {
        "device_limit": [3, 5],
        "traffic_limit_bytes": [0, 10 * 1024**3],
        "traffic_reset_strategy": [None, "month_from_creation"],
    }


async def test_4_6_user_deleted_in_panel_marks_subscription_deleted(
    db_session: AsyncSession,
) -> None:
    """4.6: пользователя удалили в панели — подписка помечена удалённой, история платежей
    сохраняется, событие в журнале."""
    _client, subscription, payment = await _subscription(db_session)

    await _reconcile(db_session, subscription, FakePanel())

    assert subscription.deleted_at is not None
    assert await db_session.get(Payment, payment.id) is not None
    assert len(await _segments(db_session, subscription)) == 1
    assert [action for action, _ in await _journal(db_session, subscription)] == [
        "subscription.deleted_in_panel"
    ]


async def test_deleted_subscription_is_not_reconciled_again(db_session: AsyncSession) -> None:
    """Удалённая подписка больше не сверяется: в панели её пользователя нет."""
    _client, subscription, _payment = await _subscription(db_session)
    subscription.deleted_at = T1
    panel = FakePanel()

    await _reconcile(db_session, subscription, panel)

    assert panel.requests == []


async def test_4_7_disabled_and_enabled_in_panel(db_session: AsyncSession) -> None:
    """4.7: подписку отключили в панели — магазин знает это; включили — снова обычное состояние."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel({7: user_json(expireAt=_iso(T1), status="DISABLED")})

    await _reconcile(db_session, subscription, panel)
    assert subscription.panel_status == PanelUserStatus.DISABLED

    panel.users[7]["status"] = "ACTIVE"
    await _reconcile(db_session, subscription, panel)
    assert subscription.panel_status == PanelUserStatus.ACTIVE

    changes = [details["changes"] for _, details in await _journal(db_session, subscription)]
    assert changes == [{"status": ["active", "disabled"]}, {"status": ["disabled", "active"]}]


async def test_4_29_revoked_link_is_saved_and_client_gets_new_one(
    db_session: AsyncSession,
) -> None:
    """4.29: ссылку отозвали в панели — магазин сохраняет новую и ставит клиенту сообщение
    с новой ссылкой и кнопкой копирования."""
    client, subscription, _payment = await _subscription(db_session)
    new_link = "https://sub.example.com/zzz999"
    panel = FakePanel(
        {7: user_json(expireAt=_iso(T1), shortUuid="zzz999", subscriptionUrl=new_link)}
    )

    await _reconcile(db_session, subscription, panel)

    assert (subscription.panel_short_uuid, subscription.subscription_url) == ("zzz999", new_link)
    [message] = [
        SendArgs.model_validate(args) for args in await db_session.scalars(select(QueueTask.args))
    ]
    assert (message.client_id, message.text_key) == (client.id, "event.link_revoked")
    assert message.variables == {"subscription_name": "Основная", "subscription_link": new_link}
    assert [(b.text_key, b.copy_text) for b in message.buttons] == [("btn.copy_link", new_link)]


async def test_4_29_new_link_domain_without_revocation_sends_nothing(
    db_session: AsyncSession,
) -> None:
    """Оператор сменил домен ссылок в панели, ссылку никто не отзывал — адрес обновляется
    без сообщения клиенту."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel(
        {7: user_json(expireAt=_iso(T1), subscriptionUrl="https://new.example.com/abc123")}
    )

    await _reconcile(db_session, subscription, panel)

    assert subscription.subscription_url == "https://new.example.com/abc123"
    assert (await db_session.scalars(select(QueueTask))).all() == []


async def test_4_8_nothing_changed_means_no_journal_entry(db_session: AsyncSession) -> None:
    """4.8, 4.12: сверка без расхождений ничего не пишет в журнал, но обновляет последние
    известные данные — их клиент видит, пока панель недоступна."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel({7: user_json(expireAt=_iso(T1))})

    await _reconcile(
        db_session, subscription, panel, ReconcileArgs(subscription_id=0, source=Source.SYNC)
    )

    assert await _journal(db_session, subscription) == []
    assert subscription.panel_synced_at is not None
    assert subscription.traffic_used_bytes == 1024


async def test_4_9_change_found_by_sync_is_journaled_as_sync(db_session: AsyncSession) -> None:
    """4.9: изменение, найденное периодической сверкой, в журнале — с путём «сверка»."""
    _client, subscription, _payment = await _subscription(db_session)
    panel = FakePanel({7: user_json(expireAt=_iso(T2))})

    await _reconcile(
        db_session, subscription, panel, ReconcileArgs(subscription_id=0, source=Source.SYNC)
    )

    [(_action, details)] = await _journal(db_session, subscription)
    assert (details["source"], details["event"]) == ("sync", None)


async def test_4_30_panel_unavailable_leaves_subscription_as_is(db_session: AsyncSession) -> None:
    """4.30: панель недоступна — сверка ждёт её («ждёт панель»), знание магазина не меняется."""
    _client, subscription, _payment = await _subscription(db_session)

    with pytest.raises(PanelUnavailableError):
        await _reconcile(db_session, subscription, FakePanel(down=True))

    assert len(await _segments(db_session, subscription)) == 1
    assert subscription.deleted_at is None


async def test_4_17_reconcile_runs_after_operations_of_subscription(
    db_session: AsyncSession,
) -> None:
    """4.17: сверка ставится с ключом подписки — после её операций, а не вперёд них."""
    _client, subscription, _payment = await _subscription(db_session)

    await enqueue_reconcile(db_session, subscription.id, Source.WEBHOOK, "user.modified")

    keys = (await db_session.scalars(select(QueueTask.key))).all()
    assert keys == [f"subscription:{subscription.id}"]


# --- Периодическая сверка всех подписок (4.8) ---


async def _run_page(session: AsyncSession, args: SyncPageArgs) -> None:
    await sync_page.run(
        TaskContext(session=session, task_id=1, attempt_number=1), args.model_dump(mode="json")
    )


async def _queued(session: AsyncSession) -> list[tuple[str, dict[str, object]]]:
    rows = await session.execute(select(QueueTask.name, QueueTask.args).order_by(QueueTask.id))
    return [(name, dict(args)) for name, args in rows]


async def test_4_8_sync_reconciles_every_live_subscription_page_by_page(
    db_session: AsyncSession,
) -> None:
    """4.8, 4.28: периодическая сверка ставит сверку каждой не удалённой подписки —
    страницами, небольшими операциями; подписке, у которой сверка уже ждёт, вторая не
    ставится."""
    client = await make_client(db_session)
    live = [make_subscription(client, None, panel_user_id=n) for n in (1, 2, 3)]
    deleted = make_subscription(client, None, panel_user_id=4)
    deleted.deleted_at = T1
    await add(db_session, *live, deleted)
    first, second, third = (s.id for s in live)
    await enqueue_reconcile(db_session, third, Source.WEBHOOK, "user.modified")

    await _run_page(db_session, SyncPageArgs(page_size=2))
    await _run_page(db_session, SyncPageArgs(after_id=second, page_size=2))

    assert await _queued(db_session) == [
        (
            "panel.reconcile_subscription",
            {"subscription_id": third, "source": "webhook", "event": "user.modified"},
        ),
        (
            "panel.reconcile_subscription",
            {"subscription_id": first, "source": "sync", "event": None},
        ),
        (
            "panel.reconcile_subscription",
            {"subscription_id": second, "source": "sync", "event": None},
        ),
        ("panel.sync_page", {"after_id": second, "page_size": 2}),
    ]


async def test_4_8_sync_interval_comes_from_settings(db_session: AsyncSession) -> None:
    """4.8: интервал сверки — из настроек (по умолчанию 15 минут), меняется без перезапуска."""
    member = make_team_member()
    await add(db_session, member)

    assert await sync_interval(db_session) == timedelta(minutes=15)
    await set_setting(db_session, PANEL_SYNC_INTERVAL, timedelta(minutes=5), member_id=member.id)
    assert await sync_interval(db_session) == timedelta(minutes=5)


# --- Недоступность панели (4.11–4.13, 4.30) ---


async def _check(session: AsyncSession, panel: FakePanel) -> None:
    with fake_runtime(panel=panel):
        await health_check.run(TaskContext(session=session, task_id=1, attempt_number=1), {})
    await session.flush()


async def _notifications(session: AsyncSession) -> list[SendArgs]:
    rows = await session.scalars(
        select(QueueTask.args).where(QueueTask.name == "messages.send").order_by(QueueTask.id)
    )
    return [SendArgs.model_validate(args) for args in rows]


async def _owner(session: AsyncSession) -> None:
    await add(session, TeamMember(telegram_id=1, role=TeamRole.OWNER))


async def test_4_13_long_outage_notifies_team_once(db_session: AsyncSession) -> None:
    """4.12, 4.13, 4.30: панель недоступна — простой начался, данные клиента «могут быть
    неактуальны»; дольше 5 минут — одно уведомление команде на весь простой."""
    await _owner(db_session)
    panel = FakePanel(down=True)

    await _check(db_session, panel)
    outage = await current_outage(db_session)
    assert outage is not None
    assert not await panel_available(db_session)
    assert await _notifications(db_session) == []

    outage.started_at = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    await _check(db_session, panel)
    await _check(db_session, panel)

    [notification] = await _notifications(db_session)
    assert (notification.chat_id, notification.text_key) == (1, "team.panel_unavailable")
    # Время — в часовом поясе магазина (по умолчанию Москва), месяц словом (0044)
    assert notification.variables == {"since": "1 октября 2026, 23:00 (МСК)"}


async def test_4_13_short_outage_ends_without_notification(db_session: AsyncSession) -> None:
    """4.13: панель вернулась раньше 5 минут — уведомления нет; простой закрыт, данные снова
    актуальны; следующий простой — новый, со своим уведомлением."""
    await _owner(db_session)
    panel = FakePanel(down=True)

    await _check(db_session, panel)
    panel.down = False
    await _check(db_session, panel)

    assert await panel_available(db_session)
    assert await _notifications(db_session) == []
    actions = (
        await db_session.scalars(
            select(JournalEntry.action).where(JournalEntry.action.like("panel.%"))
        )
    ).all()
    assert actions == ["panel.unavailable", "panel.available_again"]


async def test_4_13_alert_delay_comes_from_settings(db_session: AsyncSession) -> None:
    """4.13: через сколько уведомлять о простое — настройка оператора."""
    member = make_team_member(telegram_id=1)
    await add(db_session, member)
    await set_setting(
        db_session, PANEL_OUTAGE_ALERT_AFTER, timedelta(seconds=1), member_id=member.id
    )
    panel = FakePanel(down=True)

    await _check(db_session, panel)
    outage = await current_outage(db_session)
    assert outage is not None
    outage.started_at = datetime.now(UTC) - timedelta(seconds=2)
    await _check(db_session, panel)

    assert [n.text_key for n in await _notifications(db_session)] == ["team.panel_unavailable"]


async def test_4_30_error_answer_is_not_an_outage(db_session: AsyncSession) -> None:
    """4.30: панель ответила ошибкой (например, неверный токен) — это не простой: до неё
    достучались. Проверка связи при этом не падает."""
    await _check(db_session, FakePanel(error_status=401))

    assert await panel_available(db_session)
