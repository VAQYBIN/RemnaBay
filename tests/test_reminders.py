"""Напоминания и сообщения о сроке подписки (блок 7, 7.1–7.19; 1.22; решения 0014,
0061, 0063, 0064)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._shop import Renew
from remnabay.domain.clients import Client
from remnabay.domain.reminders import Reminder, ReminderState
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import TariffState, TariffType
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Subject, entries_for
from remnabay.messaging import RecipientBlockedError, SendArgs, send_message
from remnabay.panel import PanelUnavailableError
from remnabay.panel_sync import ReconcileArgs, Source, reconcile, reconcile_subscription
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask, TaskStatus
from remnabay.reminders import (
    PAID_SCHEDULE,
    TRIAL_SCHEDULE,
    ReplanArgs,
    ScheduleError,
    dispatch_due,
    plan_new,
    replan_reminders,
    save_schedules,
    send_key,
    send_reminders,
)
from remnabay.shop import SHOP_STATE, SHOP_TESTERS, ShopState
from remnabay.worker.main import PERIODIC, TASKS
from tests.domain_support import add, make_client, make_subscription, make_tariff, put_setting
from tests.guide_support import queued, run_queued
from tests.panel_support import FakePanel, FakeSender, fake_runtime
from tests.web_support import Shop, running_shop

OWNER_TELEGRAM_ID = 1


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(moment: datetime | None) -> str:
    assert moment is not None
    return moment.isoformat().replace("+00:00", ".000Z")


@pytest.fixture
async def session(db_session: AsyncSession) -> AsyncSession:
    await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
    return db_session


class World:
    """Клиент, подписка и та же подписка в подставной панели."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.panel = FakePanel()
        self._next_user = 7

    async def subscription(
        self,
        ends_in: timedelta,
        *,
        client: Client | None = None,
        telegram_id: int = 100,
        trial: bool = False,
        status: PanelUserStatus = PanelUserStatus.ACTIVE,
        tariff_state: TariffState | None = None,
        tariff_type: TariffType = TariffType.TERM_UNLIMITED,
    ) -> Subscription:
        """Подписка, которая заканчивается через `ends_in` от текущего момента."""
        if client is None:
            client = await make_client(self.session, telegram_id=telegram_id)
        tariff = None
        if tariff_state is not None:
            package = {"duration_days": None, "traffic_limit_bytes": 10 * 1024**3}
            extra = package if tariff_type == TariffType.PACKAGE_ONLY else {}
            tariff = make_tariff(state=tariff_state, type=tariff_type, **extra)
            await add(self.session, tariff)
        user_id = self._next_user
        self._next_user += 1
        subscription = make_subscription(client, tariff, panel_user_id=user_id)
        subscription.is_trial = trial
        subscription.expires_at = _now() + ends_in
        subscription.panel_status = status
        await add(self.session, subscription)
        self.panel.add_user(
            id=user_id,
            shortUuid=subscription.panel_short_uuid,
            subscriptionUrl=subscription.subscription_url,
            expireAt=_iso(subscription.expires_at),
            status=status.value.upper(),
        )
        return subscription

    def panel_user(self, subscription: Subscription) -> dict[str, object]:
        return self.panel.users[subscription.panel_user_id]

    async def plan_long_ago(self) -> None:
        """Расписание запланировано заранее: все моменты тогда были в будущем."""
        await plan_new(self.session, _now() - timedelta(days=60))
        await self.session.flush()

    async def tick(self) -> list[SendArgs]:
        """Проход раз в минуту и отправки, которые он поставил; сообщения, ушедшие в очередь."""
        before = {task.id for task in await queued(self.session, send_message)}
        await plan_new(self.session, _now())
        await dispatch_due(self.session, _now())
        with fake_runtime(panel=self.panel):
            await run_queued(self.session, send_reminders)
        return [
            SendArgs.model_validate(task.args)
            for task in await queued(self.session, send_message)
            if task.id not in before
        ]

    async def reminders(self, subscription: Subscription) -> dict[int, ReminderState]:
        """Напоминания по текущей дате окончания: смещение → состояние."""
        await self.session.flush()
        # Планирование пишет строки запросом INSERT … ON CONFLICT мимо объектов сессии
        rows = await self.session.scalars(
            select(Reminder)
            .where(
                Reminder.subscription_id == subscription.id,
                Reminder.expires_at == subscription.expires_at,
            )
            .execution_options(populate_existing=True)
        )
        return {row.offset_hours: row.state for row in rows}


@pytest.fixture
def world(session: AsyncSession) -> World:
    return World(session)


def _buttons(message: SendArgs) -> list[tuple[str, str | None, str | None]]:
    return [(b.text_key, b.callback_data, b.style) for b in message.buttons]


S, SENT, SKIPPED, CANCELLED = (
    ReminderState.SCHEDULED,
    ReminderState.SENT,
    ReminderState.SKIPPED,
    ReminderState.CANCELLED,
)


# --- Расписание ---


async def test_7_1_7_2_default_schedules(world: World) -> None:
    """7.1, 7.2: по умолчанию платной — за 72 ч, за 24 ч, через 72 ч и «Подписка истекла»
    в момент окончания; триалу — за 24 ч и «Пробный период закончился»."""
    paid = await world.subscription(timedelta(days=10))
    trial = await world.subscription(timedelta(days=10), telegram_id=101, trial=True)
    await plan_new(world.session, _now())

    assert await world.reminders(paid) == {72: S, 24: S, 0: S, -72: S}
    assert await world.reminders(trial) == {24: S, 0: S}


async def test_7_4_due_at_is_end_minus_offset(world: World) -> None:
    """7.4: момент отправки — дата окончания минус смещение, ровно N часов."""
    subscription = await world.subscription(timedelta(days=10))
    await plan_new(world.session, _now())
    rows = await world.session.scalars(
        select(Reminder).where(Reminder.subscription_id == subscription.id)
    )
    assert subscription.expires_at is not None
    assert {row.offset_hours: row.due_at for row in rows} == {
        offset: subscription.expires_at - timedelta(hours=offset) for offset in (72, 24, 0, -72)
    }


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        owner = TeamMember(telegram_id=OWNER_TELEGRAM_ID, role=TeamRole.OWNER)
        await add(db_session, owner)
        await shop.sign_in(owner)
        yield shop


async def test_7_1_7_2_settings_api_collapses_duplicates_and_keeps_limits(shop: Shop) -> None:
    """7.1, 7.2, решение 0061: владелец задаёт оба расписания; повторы схлопываются,
    порядок — по моментам; 0, больше 720 часов и больше 10 смещений не принимаются."""
    response = await shop.http.get("/api/admin/settings/reminders")
    assert response.json() == {"paid": [72, 24, -72], "trial": [24]}

    saved = await shop.http.put(
        "/api/admin/settings/reminders",
        json={"paid": [-24, 48, 48, 720, -720], "trial": [12, -24]},
    )
    assert saved.status_code == 200
    assert saved.json() == {"paid": [720, 48, -24, -720], "trial": [12, -24]}

    for paid in ([0], [721], [-721], list(range(1, 12))):
        rejected = await shop.http.put(
            "/api/admin/settings/reminders", json={"paid": paid, "trial": []}
        )
        assert rejected.status_code == 422, paid
    assert (await shop.http.get("/api/admin/settings/reminders")).json()["paid"] == [
        720,
        48,
        -24,
        -720,
    ]


async def test_7_15_zero_offset_is_rejected(session: AsyncSession) -> None:
    """7.15, решение 0061: смещения 0 нет — в момент окончания приходит только «Подписка
    истекла»."""
    with pytest.raises(ScheduleError, match="Смещения 0 нет"):
        await save_schedules(session, [24, 0], [24], member_id=1)


async def test_7_3_schedule_change_replans_without_restart(world: World) -> None:
    """7.3: изменение расписания применяется сразу: убранное смещение отменяется, новое
    планируется; прошедший момент нового смещения не отправляется (7.18). Изменение —
    в журнале."""
    owner = TeamMember(telegram_id=OWNER_TELEGRAM_ID, role=TeamRole.OWNER)
    await add(world.session, owner)
    subscription = await world.subscription(timedelta(hours=100))
    await plan_new(world.session, _now())
    assert await world.reminders(subscription) == {72: S, 24: S, 0: S, -72: S}

    await save_schedules(world.session, [120, 48], [24], member_id=owner.id)
    assert await world.reminders(subscription) == {
        72: CANCELLED,
        24: CANCELLED,
        0: S,
        -72: CANCELLED,
    }
    with fake_runtime(panel=world.panel):
        assert await run_queued(world.session, replan_reminders) == 1

    assert await world.reminders(subscription) == {
        120: SKIPPED,
        72: CANCELLED,
        48: S,
        24: CANCELLED,
        0: S,
        -72: CANCELLED,
    }
    changes = await entries_for(world.session, Subject("setting", PAID_SCHEDULE.key))
    assert [entry.details["new"] for entry in changes] == [[120, 48]]
    assert not await entries_for(world.session, Subject("setting", TRIAL_SCHEDULE.key))

    # Вернули смещение — запланировано снова
    await save_schedules(world.session, [120, 48, 72], [24], member_id=owner.id)
    with fake_runtime(panel=world.panel):
        await run_queued(world.session, replan_reminders)
    assert (await world.reminders(subscription))[72] == S


async def test_7_3_replan_goes_by_pages(world: World) -> None:
    """7.3, 4.28: пересчёт большого числа подписок делится на страницы."""
    first = await world.subscription(timedelta(days=10))
    second = await world.subscription(timedelta(days=10), telegram_id=101)
    with fake_runtime(panel=world.panel):
        await replan_reminders.run(
            TaskContext(world.session, 1, 1), ReplanArgs(page_size=1).model_dump()
        )
    assert await world.reminders(first) == {72: S, 24: S, 0: S, -72: S}
    assert await world.reminders(second) == {}
    [next_page] = await queued(world.session, replan_reminders)
    assert next_page.args == {"after_id": first.id, "page_size": 1}


# --- Планирование и отправка ---


async def test_7_5_reminder_names_subscription_date_and_renew_button(world: World) -> None:
    """7.5: напоминание — название подписки, дата окончания и «Продлить» этой подписки."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()

    [message] = await world.tick()

    assert message.text_key == "reminder.before"
    assert message.extra_keys == []
    assert message.variables == {"subscription_name": "Основная"}
    assert message.dates == {"end_date": subscription.expires_at}
    assert _buttons(message) == [
        ("btn.renew", f"renew:{subscription.id}", "success"),
        ("btn.main_menu", "menu:main", None),
    ]
    # Кнопку разбирает тот же обработчик, что «Продлить» в карточке подписки
    assert Renew(subscription_id=subscription.id).pack() == f"renew:{subscription.id}"
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SENT, 0: S, -72: S}
    [entry] = await entries_for(world.session, Subject("subscription", subscription.id))
    assert entry.action == "reminder.sent"
    assert entry.details["key"] == "reminder.before"
    assert entry.details["skipped_offsets"] == [72]


async def test_7_6_date_differs_in_panel_replans(world: World) -> None:
    """7.6, 7.9: дата окончания в панели другая — напоминание не отправляется, сверка
    принимает новую дату, расписание пересчитывается от неё."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    new_end = _now() + timedelta(days=30)
    world.panel_user(subscription)["expireAt"] = _iso(new_end)

    assert await world.tick() == []
    # Наступившие отменены; будущие по старой дате заменит пересчёт после сверки
    assert await world.reminders(subscription) == {72: CANCELLED, 24: CANCELLED, 0: S, -72: S}
    [check] = await queued(world.session, reconcile_subscription)
    assert check.args["event"] == "reminder"

    with fake_runtime(panel=world.panel):
        await run_queued(world.session, reconcile_subscription)
    assert subscription.expires_at == new_end
    assert await world.tick() == []
    assert await world.reminders(subscription) == {72: S, 24: S, 0: S, -72: S}


async def test_7_6_panel_unavailable_postpones_then_only_latest(world: World) -> None:
    """7.6: панель недоступна — отправка ждёт её восстановления; из наступивших за это
    время уходит только самое позднее."""
    subscription = await world.subscription(-timedelta(minutes=5))
    await world.plan_long_ago()
    await dispatch_due(world.session, _now())
    world.panel.down = True
    [task] = await queued(world.session, send_reminders)
    with fake_runtime(panel=world.panel), pytest.raises(PanelUnavailableError):
        async with world.session.begin_nested():
            await send_reminders.run(TaskContext(world.session, task.id, 1), task.args)
    assert await world.reminders(subscription) == {72: S, 24: S, 0: S, -72: S}
    # Пока отправка ждёт панель, вторая не ставится
    await dispatch_due(world.session, _now())
    assert len(await queued(world.session, send_reminders)) == 1

    world.panel.down = False
    with fake_runtime(panel=world.panel):
        await run_queued(world.session, send_reminders)

    [message] = [
        SendArgs.model_validate(task.args) for task in await queued(world.session, send_message)
    ]
    assert message.text_key == "event.expired"
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: SENT, -72: S}


async def test_7_6_exhausted_traffic_does_not_stop_reminders(world: World) -> None:
    """7.6, решение 0061: исчерпанный трафик напоминаниям не мешает."""
    await world.subscription(timedelta(hours=23), status=PanelUserStatus.LIMITED)
    await world.plan_long_ago()
    [message] = await world.tick()
    assert message.text_key == "reminder.before"


async def test_7_7_after_downtime_only_latest_including_expired(world: World) -> None:
    """7.7, решение 0061: магазин был недоступен — из наступивших отправляется только самое
    позднее; «Подписка истекла» участвует наравне с напоминаниями."""
    subscription = await world.subscription(-timedelta(hours=1))
    await world.plan_long_ago()

    [message] = await world.tick()

    assert message.text_key == "event.expired"
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: SENT, -72: S}


async def test_7_8_sent_once_even_after_repeated_ticks(world: World) -> None:
    """7.8: отправленное напоминание повторно не отправляется, пока дата окончания та же."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    assert len(await world.tick()) == 1
    assert await world.tick() == []
    # Повторное планирование той же даты не возвращает отправленное
    await world.plan_long_ago()
    assert await world.tick() == []
    assert (await world.reminders(subscription))[24] == SENT


async def test_7_8_failure_after_marking_does_not_resend(world: World) -> None:
    """7.8, сквозное правило 4: отметка «отправлено» и сообщение — одна транзакция;
    если она откатилась, не ушло ни то, ни другое, и следующий проход отправит заново."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    await dispatch_due(world.session, _now())
    [task] = await queued(world.session, send_reminders)

    class CrashError(Exception):
        pass

    async def send_then_crash() -> None:
        async with world.session.begin_nested():
            await send_reminders.run(TaskContext(world.session, task.id, 1), task.args)
            raise CrashError

    with fake_runtime(panel=world.panel), pytest.raises(CrashError):
        await send_then_crash()
    assert (await world.reminders(subscription))[24] == S
    assert await queued(world.session, send_message) == []


async def test_7_9_renewal_replans_from_new_date(world: World) -> None:
    """7.9: дата окончания изменилась (продление) — напоминания планируются заново."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    assert len(await world.tick()) == 1
    old_end = subscription.expires_at
    subscription.expires_at = _now() + timedelta(days=30)
    world.panel_user(subscription)["expireAt"] = _iso(subscription.expires_at)

    assert await world.tick() == []

    assert await world.reminders(subscription) == {72: S, 24: S, 0: S, -72: S}
    rows = await world.session.execute(
        select(Reminder.offset_hours, Reminder.state).where(
            Reminder.subscription_id == subscription.id, Reminder.expires_at == old_end
        )
    )
    old_states = {offset: state for offset, state in rows}
    assert old_states == {72: SKIPPED, 24: SENT, 0: CANCELLED, -72: CANCELLED}


async def test_7_10_each_subscription_has_own_reminders(world: World) -> None:
    """7.10: у каждой подписки свои напоминания."""
    first = await world.subscription(timedelta(hours=23))
    second = await world.subscription(timedelta(hours=23), client=None, telegram_id=101)
    await world.plan_long_ago()

    messages = await world.tick()

    assert sorted(message.variables["subscription_name"] for message in messages) == [
        "Основная",
        "Основная",
    ]
    assert {message.client_id for message in messages} == {first.client_id, second.client_id}


# --- Содержание ---


async def test_7_11_archived_tariff_warns_before_end_only(world: World) -> None:
    """7.11: тариф в архиве — напоминание до окончания предупреждает, что потом нужно
    будет выбрать новый; после окончания предупреждения нет."""
    before = await world.subscription(timedelta(hours=23), tariff_state=TariffState.ARCHIVED)
    await world.plan_long_ago()
    [message] = await world.tick()
    assert message.text_key == "reminder.before"
    assert message.extra_keys == ["reminder.before.archived"]
    assert message.client_id == before.client_id

    after = await world.subscription(
        -timedelta(hours=73), telegram_id=101, tariff_state=TariffState.ARCHIVED
    )
    await world.plan_long_ago()
    [message] = await world.tick()
    assert message.client_id == after.client_id
    assert message.text_key == "reminder.after"
    assert message.extra_keys == []


async def test_7_12_expired_message_with_renew(world: World) -> None:
    """7.12, С8: подписка истекла — «Подписка истекла» с «Продлить»."""
    subscription = await world.subscription(-timedelta(minutes=1))
    await world.plan_long_ago()
    # Напоминания до окончания уже ушли в свой срок
    await world.session.execute(
        update(Reminder)
        .where(Reminder.subscription_id == subscription.id, Reminder.offset_hours > 0)
        .values(state=ReminderState.SENT)
    )

    [message] = await world.tick()

    assert message.text_key == "event.expired"
    assert _buttons(message) == [
        ("btn.renew", f"renew:{subscription.id}", "success"),
        ("btn.main_menu", "menu:main", None),
    ]


async def test_7_12_late_expiry_replaced_by_later_reminder(world: World) -> None:
    """7.12, решение 0061: истечение обнаружено с опозданием, когда уже наступило
    напоминание «через 72 часа», — отправляется оно, «Подписка истекла» пропускается."""
    subscription = await world.subscription(-timedelta(hours=73))
    await world.plan_long_ago()

    [message] = await world.tick()

    assert message.text_key == "reminder.after"
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: SKIPPED, -72: SENT}


async def test_7_12_renewed_before_end_gets_no_expired(world: World) -> None:
    """7.12: подписку продлили до момента окончания — «Подписка истекла» не приходит."""
    subscription = await world.subscription(-timedelta(minutes=1))
    await world.plan_long_ago()
    world.panel_user(subscription)["expireAt"] = _iso(_now() + timedelta(days=30))

    assert await world.tick() == []


async def test_7_13_trial_end_offers_purchase(world: World) -> None:
    """7.13, С9: по окончании триала — одно сообщение «Пробный период закончился» с
    «Купить подписку», без «Подписка истекла»."""
    subscription = await world.subscription(-timedelta(minutes=1), trial=True)
    await world.plan_long_ago()

    [message] = await world.tick()

    assert message.text_key == "event.trial_ended"
    assert _buttons(message) == [
        ("btn.buy", "buy", "success"),
        ("btn.main_menu", "menu:main", None),
    ]
    assert await world.reminders(subscription) == {24: SKIPPED, 0: SENT}


async def test_7_2_trial_reminder_texts_before_and_after(world: World) -> None:
    """7.2, 7.13, решение 0063: у триала свои тексты до и после окончания; напоминание
    после окончания — только если задано в расписании."""
    await put_setting(world.session, TRIAL_SCHEDULE, [24, -48])
    before = await world.subscription(timedelta(hours=23), trial=True)
    await world.plan_long_ago()
    [message] = await world.tick()
    assert (message.client_id, message.text_key) == (before.client_id, "reminder.trial_before")
    assert _buttons(message)[0] == ("btn.buy", "buy", "success")

    after = await world.subscription(-timedelta(hours=49), telegram_id=101, trial=True)
    await world.plan_long_ago()
    [message] = await world.tick()
    assert (message.client_id, message.text_key) == (after.client_id, "reminder.trial_after")
    assert message.dates == {"end_date": after.expires_at}


async def test_7_16_7_19_disabled_then_enabled_sends_only_latest(world: World) -> None:
    """7.16: отключённой в панели подписке напоминания не отправляются. 7.19: когда её
    снова включили, отправляется одно — самое позднее из наступивших за время отключения."""
    subscription = await world.subscription(-timedelta(hours=1), status=PanelUserStatus.DISABLED)
    await world.plan_long_ago()
    assert await world.tick() == []
    assert await world.reminders(subscription) == {72: S, 24: S, 0: S, -72: S}

    world.panel_user(subscription)["status"] = "EXPIRED"
    with fake_runtime(panel=world.panel):
        await reconcile(
            world.session, subscription, ReconcileArgs(subscription_id=0, source=Source.WEBHOOK)
        )
    [message] = await world.tick()

    assert message.text_key == "event.expired"
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: SENT, -72: S}


async def test_7_16_disabled_in_panel_but_not_yet_known(world: World) -> None:
    """7.16: панель уже отключила подписку, а магазин ещё не знает — сообщение не уходит,
    ставится сверка; напоминания ждут включения."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    world.panel_user(subscription)["status"] = "DISABLED"

    assert await world.tick() == []
    assert (await world.reminders(subscription))[24] == S
    assert len(await queued(world.session, reconcile_subscription)) == 1


async def test_7_19_renewed_while_disabled_sends_nothing(world: World) -> None:
    """7.19: если за время отключения подписку продлили, досылать нечего."""
    subscription = await world.subscription(-timedelta(hours=1), status=PanelUserStatus.DISABLED)
    await world.plan_long_ago()
    assert await world.tick() == []
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = _now() + timedelta(days=30)
    world.panel_user(subscription).update(status="ACTIVE", expireAt=_iso(subscription.expires_at))

    assert await world.tick() == []


async def test_7_14_undelivered_reminder_is_journaled_not_resent(world: World) -> None:
    """7.14: недоставленное напоминание записывается в журнал и повторно не отправляется."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    await world.tick()
    sender = FakeSender(errors=[RecipientBlockedError("бот заблокирован")])
    with fake_runtime(sender=sender):
        await run_queued(world.session, send_message)

    entries = await entries_for(world.session, Subject("client", subscription.client_id))
    assert [(e.action, e.details["key"]) for e in entries] == [
        ("message.not_delivered", "reminder.before")
    ]
    assert await world.tick() == []
    assert (await world.reminders(subscription))[24] == SENT


async def test_7_18_past_moment_at_planning_is_not_sent(world: World) -> None:
    """7.18, решение 0063: момент, который уже прошёл, когда его планируют (первый запуск,
    подписка короче смещения), не отправляется."""
    subscription = await world.subscription(timedelta(hours=10))
    expired_long_ago = await world.subscription(-timedelta(days=10), telegram_id=101)

    assert await world.tick() == []

    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: S, -72: S}
    assert await world.reminders(expired_long_ago) == {
        72: SKIPPED,
        24: SKIPPED,
        0: SKIPPED,
        -72: SKIPPED,
    }


async def test_7_18_term_shortened_to_past_sends_no_expired(world: World) -> None:
    """7.18, решение 0064: команда в панели сократила срок до прошлого — «Подписка истекла»
    не отправляется; напоминание после окончания, чей момент впереди, приходит как обычно."""
    subscription = await world.subscription(timedelta(days=10))
    await world.plan_long_ago()
    new_end = _now() - timedelta(hours=1)
    world.panel_user(subscription)["expireAt"] = _iso(new_end)

    with fake_runtime(panel=world.panel):
        await reconcile(
            world.session, subscription, ReconcileArgs(subscription_id=0, source=Source.SYNC)
        )
    assert subscription.expires_at == new_end

    assert await world.tick() == []
    assert await world.reminders(subscription) == {72: SKIPPED, 24: SKIPPED, 0: SKIPPED, -72: S}


async def test_package_only_and_deleted_subscriptions_get_nothing(world: World) -> None:
    """Сценарий «Истечение подписки»: у «только пакет» нет напоминаний по сроку; удалённая
    в панели подписка их тоже не получает (4.6)."""
    package = await world.subscription(
        timedelta(days=10), tariff_state=TariffState.ON_SALE, tariff_type=TariffType.PACKAGE_ONLY
    )
    deleted = await world.subscription(timedelta(hours=23), telegram_id=101)
    await world.plan_long_ago()
    deleted.deleted_at = _now()

    assert await world.tick() == []
    assert await world.reminders(package) == {}
    assert await world.reminders(deleted) == {72: CANCELLED, 24: CANCELLED, 0: S, -72: S}


# --- Магазин не открыт или временно закрыт ---


async def test_7_17_not_opened_only_team_and_testers(world: World) -> None:
    """7.17, решение 0061: пока магазин не открыт, сообщения о сроке получают только команда
    и тестировщики; остальным после открытия ничего не досылается."""
    await put_setting(world.session, SHOP_STATE, ShopState.NOT_OPENED)
    await put_setting(world.session, SHOP_TESTERS, [101])
    await add(world.session, TeamMember(telegram_id=102, role=TeamRole.ASSISTANT))
    client = await world.subscription(timedelta(hours=23), telegram_id=100)
    tester = await world.subscription(timedelta(hours=23), telegram_id=101)
    member = await world.subscription(timedelta(hours=23), telegram_id=102)
    await world.plan_long_ago()

    messages = await world.tick()

    assert {message.client_id for message in messages} == {tester.client_id, member.client_id}
    assert await world.reminders(client) == {72: SKIPPED, 24: SKIPPED, 0: S, -72: S}
    await put_setting(world.session, SHOP_STATE, ShopState.OPEN)
    assert await world.tick() == []


async def test_1_22_paused_shop_keeps_silent_then_sends_only_latest(world: World) -> None:
    """1.22, решение 0061: пока магазин временно закрыт, сообщения о сроке не отправляются
    (кроме команды и тестировщиков); после открытия по подписке уходит одно — самое
    позднее из наступивших."""
    await put_setting(world.session, SHOP_STATE, ShopState.PAUSED)
    await put_setting(world.session, SHOP_TESTERS, [101])
    client = await world.subscription(-timedelta(hours=1), telegram_id=100)
    tester = await world.subscription(timedelta(hours=23), telegram_id=101)
    await world.plan_long_ago()

    [message] = await world.tick()
    assert message.client_id == tester.client_id
    assert await world.reminders(client) == {72: S, 24: S, 0: S, -72: S}

    await put_setting(world.session, SHOP_STATE, ShopState.OPEN)
    [message] = await world.tick()

    assert (message.client_id, message.text_key) == (client.client_id, "event.expired")
    assert await world.reminders(client) == {72: SKIPPED, 24: SKIPPED, 0: SENT, -72: S}


async def test_1_22_paused_shop_trial_reminder_waits(world: World) -> None:
    """1.22, решение 0061: напоминание триала, пока магазин закрыт, тоже молчит."""
    await put_setting(world.session, SHOP_STATE, ShopState.PAUSED)
    subscription = await world.subscription(timedelta(hours=23), trial=True)
    await world.plan_long_ago()
    assert await world.tick() == []
    await put_setting(world.session, SHOP_STATE, ShopState.OPEN)
    [message] = await world.tick()
    assert message.text_key == "reminder.trial_before"
    assert await world.reminders(subscription) == {24: SENT, 0: S}


async def test_1_22_after_opening_nothing_if_renewed_or_disabled(world: World) -> None:
    """1.22: после открытия ничего не досылается, если подписку за время закрытия продлили
    или отключили в панели."""
    await put_setting(world.session, SHOP_STATE, ShopState.PAUSED)
    renewed = await world.subscription(-timedelta(hours=1), telegram_id=100)
    disabled = await world.subscription(-timedelta(hours=1), telegram_id=101)
    await world.plan_long_ago()
    assert await world.tick() == []

    renewed.expires_at = _now() + timedelta(days=30)
    world.panel_user(renewed)["expireAt"] = _iso(renewed.expires_at)
    disabled.panel_status = PanelUserStatus.DISABLED
    world.panel_user(disabled)["status"] = "DISABLED"
    await put_setting(world.session, SHOP_STATE, ShopState.OPEN)

    assert await world.tick() == []


# --- Устройство планировщика ---


async def test_0014_dispatch_does_not_duplicate_waiting_send(world: World) -> None:
    """0014: отправка, которая ещё ждёт, не ставится второй раз на следующем проходе."""
    subscription = await world.subscription(timedelta(hours=23))
    await world.plan_long_ago()
    assert await dispatch_due(world.session, _now()) == 1
    assert await dispatch_due(world.session, _now()) == 0
    [task] = await queued(world.session, send_reminders)
    assert task.key == send_key(subscription.id)


async def test_0014_worker_runs_scheduler_every_minute() -> None:
    """0014: планировщик — периодическая задача воркера раз в минуту; его задачи известны
    воркеру и действиям команды."""
    [tick] = [periodic for periodic in PERIODIC if periodic.name == "reminders.tick"]
    assert tick.every == timedelta(minutes=1)
    for name in ("reminders.tick", "reminders.send", "reminders.replan"):
        assert TASKS.get(name) is not None


async def test_0014_send_task_without_due_does_nothing(world: World) -> None:
    """Отправка, поставленная до того, как сверка поменяла дату, ничего не шлёт."""
    subscription = await world.subscription(timedelta(days=10))
    await world.plan_long_ago()
    task = QueueTask(
        name=send_reminders.name,
        args={"subscription_id": subscription.id},
        status=TaskStatus.PENDING,
    )
    await add(world.session, task)
    with fake_runtime(panel=world.panel):
        await run_queued(world.session, send_reminders)
    assert world.panel.requests == []
