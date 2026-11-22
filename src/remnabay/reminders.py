"""Сообщения о сроке подписки: напоминания, «Подписка истекла», «Пробный период
закончился» (блок 7; решения 0014, 0061, 0063).

Как это устроено:
- От даты окончания подписки (последней известной от панели) магазин планирует
  строки `Reminder`: по одной на смещение расписания и одну в момент окончания
  (смещение 0) — «Подписка истекла» или «Пробный период закончился». Все они — одна
  последовательность: из наступивших отправляется только самое позднее (0061).
- Раз в минуту (`REMINDERS_TICK`) планируются подписки, чья дата окончания ещё не
  запланирована: новая подписка, продление, правка в панели (7.9). Момент, который
  уже прошёл, когда его планируют, не отправляется (7.18). Затем для подписок с
  наступившими напоминаниями ставится отправка.
- Отправка (`send_reminders`) сверяет дату окончания с панелью (7.6). Пока панель
  недоступна, отправка ждёт; пока магазин временно закрыт или подписку отключили
  в панели — напоминания копятся, а потом уходит одно, самое позднее (1.22, 7.19).
"""

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import ColumnElement, Row, and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.clients import client_telegram_id
from remnabay.domain.clients import TelegramAccount
from remnabay.domain.reminders import Reminder, ReminderState
from remnabay.domain.subscriptions import PanelUserStatus, Subscription, operations_key
from remnabay.domain.tariffs import Tariff, TariffState, TariffType
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import (
    MAIN_MENU_BUTTON,
    ButtonArgs,
    buy_button,
    renew_button,
    send_to_client,
)
from remnabay.panel import UserStatus
from remnabay.panel_sync import Source, enqueue_reconcile, reconcile_subscription
from remnabay.queue import Periodic, TaskContext, task, unfinished_keys
from remnabay.shop import ShopState, is_team_or_tester, shop_state, team_and_testers
from remnabay.shop_settings import ShopSetting, get_setting, set_setting

SUBSCRIPTION_SUBJECT = "subscription"
# В каждом расписании — до 10 смещений, от 1 до 720 часов до или после окончания (0061)
MAX_OFFSETS = 10
MAX_OFFSET_HOURS = 720
# Момент окончания: «Подписка истекла» или «Пробный период закончился» (7.12, 7.13)
END_OFFSET = 0
TICK_EVERY = timedelta(minutes=1)
# Насколько раньше прохода планирования может отсчитываться «момент уже прошёл» (7.18):
# от того, когда магазин узнал дату окончания, но не дальше этого предела — иначе после
# долгого перерыва ушли бы давно прошедшие напоминания
_LEARNED_GRACE = timedelta(minutes=5)
# Подписок за один проход: большая работа делится на части (4.28)
PLAN_BATCH = 5000
DISPATCH_BATCH = 1000
# Строк в одном INSERT: у PostgreSQL ограничено число параметров запроса
_INSERT_CHUNK = 2000
# Планирование и смена расписания идут по очереди: иначе планирование, прочитавшее
# старое расписание, могло бы дописать убранное смещение после пересчёта
_PLAN_LOCK = 0x72656D696E64


def _normalized(offsets: list[int]) -> list[int]:
    """Повторяющиеся смещения схлопываются; порядок — от самого раннего момента (0061)."""
    unique = sorted(set(offsets), reverse=True)
    if END_OFFSET in unique:
        raise ValueError(
            "Смещения 0 нет: в момент окончания приходит «Подписка истекла» "
            "или «Пробный период закончился»"
        )
    if len(unique) > MAX_OFFSETS:
        raise ValueError(f"В расписании не больше {MAX_OFFSETS} напоминаний")
    return unique


# Смещение в часах: положительное — до окончания, отрицательное — после (7.1)
type Offset = Annotated[int, Field(ge=-MAX_OFFSET_HOURS, le=MAX_OFFSET_HOURS)]
type Schedule = Annotated[list[Offset], Field(max_length=100), AfterValidator(_normalized)]

_SCHEDULE = TypeAdapter[list[int]](Schedule)
# По умолчанию: за 72 часа, за 24 часа, через 72 часа после (7.1); у триала — за 24 часа (7.2)
PAID_SCHEDULE = ShopSetting("reminders.paid", _SCHEDULE, [72, 24, -72])
TRIAL_SCHEDULE = ShopSetting("reminders.trial", _SCHEDULE, [24])


@dataclass(frozen=True)
class Schedules:
    paid: list[int]
    trial: list[int]

    def offsets(self, is_trial: bool) -> tuple[int, ...]:
        """Смещения подписки вместе с моментом окончания."""
        return (*(self.trial if is_trial else self.paid), END_OFFSET)


async def schedules(session: AsyncSession) -> Schedules:
    return Schedules(
        paid=await get_setting(session, PAID_SCHEDULE),
        trial=await get_setting(session, TRIAL_SCHEDULE),
    )


async def _lock_planning(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_PLAN_LOCK)))


# --- Планирование ---


def _plannable() -> ColumnElement[bool]:
    """Подписки, которым нужны сообщения о сроке. У «только пакет» их нет — только по
    трафику (сценарий «Истечение подписки и напоминания»)."""
    package_only = exists().where(
        Tariff.id == Subscription.tariff_id, Tariff.type == TariffType.PACKAGE_ONLY
    )
    return and_(
        Subscription.deleted_at.is_(None),
        Subscription.expires_at.is_not(None),
        ~package_only,
        ~_ended_by_refund(),
    )


def _ended_by_refund() -> ColumnElement[bool]:
    """Возврат закончил подписку сейчас: по этому сроку сообщений нет (10.13)."""
    return and_(
        Subscription.ended_by_refund_at.is_not(None),
        Subscription.expires_at <= Subscription.ended_by_refund_at,
    )


def ended_by_refund(subscription: Subscription) -> bool:
    return (
        subscription.ended_by_refund_at is not None
        and subscription.expires_at is not None
        and subscription.expires_at <= subscription.ended_by_refund_at
    )


async def silence_after_refund(
    session: AsyncSession, subscription: Subscription, end: datetime
) -> None:
    """10.13: возврат закончил подписку сейчас — ни напоминаний об окончании, ни
    «Подписка истекла»: клиент получает только сообщение о возврате. `end` — срок,
    который поставил возврат: панель не принимает дату в прошлом, и подписка
    заканчивается через минуту. Запланированное отменяется; новый срок после
    продления снова получает сообщения."""
    subscription.ended_by_refund_at = end
    await session.execute(
        update(Reminder)
        .where(
            Reminder.subscription_id == subscription.id,
            Reminder.state == ReminderState.SCHEDULED,
        )
        .values(state=ReminderState.CANCELLED)
    )


def _not_in_schedule(plan: Schedules) -> ColumnElement[bool]:
    """Запланированное напоминание не по действующей дате окончания или по смещению,
    которого в расписании больше нет."""
    return or_(
        Subscription.deleted_at.is_not(None),
        Reminder.expires_at != Subscription.expires_at,
        and_(Subscription.is_trial, Reminder.offset_hours.not_in(plan.offsets(True))),
        and_(~Subscription.is_trial, Reminder.offset_hours.not_in(plan.offsets(False))),
    )


async def _cancel_outdated(
    session: AsyncSession, plan: Schedules, subscription_ids: Sequence[int] | None = None
) -> None:
    """Дата окончания изменилась или расписание поменяли — запланированное заменяется
    новым (7.3, 7.9)."""
    scope = [Reminder.subscription_id == Subscription.id, _not_in_schedule(plan)]
    if subscription_ids is not None:
        scope.append(Subscription.id.in_(subscription_ids))
    await session.execute(
        update(Reminder)
        .where(Reminder.state == ReminderState.SCHEDULED, exists().where(*scope))
        .values(state=ReminderState.CANCELLED)
    )


async def _plan(
    session: AsyncSession,
    plan: Schedules,
    subscriptions: Sequence[_Row],
    now: datetime,
    learned_at: Mapping[int, datetime] | None = None,
) -> None:
    """Момент каждого напоминания — дата окончания минус смещение (7.4). Момент, который
    уже прошёл, когда магазин узнал дату (`learned_at`, иначе — `now`), не отправляется
    (7.18). Уже отправленное по этой дате окончания повторно не планируется (7.8);
    отменённое раньше — планируется заново."""
    rows: list[dict[str, object]] = []
    for subscription_id, is_trial, expires_at in subscriptions:
        planned_from = (learned_at or {}).get(subscription_id, now)
        for offset in plan.offsets(is_trial):
            due_at = expires_at - timedelta(hours=offset)
            state = ReminderState.SCHEDULED if due_at > planned_from else ReminderState.SKIPPED
            rows.append(
                {
                    "subscription_id": subscription_id,
                    "offset_hours": offset,
                    "expires_at": expires_at,
                    "due_at": due_at,
                    "state": state,
                }
            )
    for start in range(0, len(rows), _INSERT_CHUNK):
        statement = insert(Reminder).values(rows[start : start + _INSERT_CHUNK])
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    Reminder.subscription_id,
                    Reminder.expires_at,
                    Reminder.offset_hours,
                ],
                set_={"state": statement.excluded.state},
                where=Reminder.state == ReminderState.CANCELLED,
            )
        )
    await _cancel_outdated(session, plan, [row[0] for row in subscriptions])


type _Row = tuple[int, bool, datetime]


def _with_end(rows: Sequence[Row[int, bool, datetime | None]]) -> list[_Row]:
    result: list[_Row] = []
    for subscription_id, is_trial, expires_at in rows:
        if expires_at is not None:
            result.append((subscription_id, is_trial, expires_at))
    return result


async def plan_new(session: AsyncSession, now: datetime) -> int:
    """Планирует подписки, чья текущая дата окончания ещё не запланирована: признак —
    нет действующего (не отменённого) сообщения в момент окончания по этой дате.

    «Момент уже прошёл» (7.18) отсчитывается от того, когда магазин узнал дату, а не от
    прохода: дата, которую узнали за минуту до момента напоминания, — не повод его
    пропустить (7.9). Возвращает число подписок."""
    await _lock_planning(session)
    planned_end = exists().where(
        Reminder.subscription_id == Subscription.id,
        Reminder.expires_at == Subscription.expires_at,
        Reminder.offset_hours == END_OFFSET,
        Reminder.state != ReminderState.CANCELLED,
    )
    rows = (
        await session.execute(
            select(
                Subscription.id,
                Subscription.is_trial,
                Subscription.expires_at,
                Subscription.panel_synced_at,
            )
            .where(_plannable(), ~planned_end)
            .order_by(Subscription.id)
            .limit(PLAN_BATCH)
        )
    ).all()
    subscriptions: list[_Row] = [
        (subscription_id, is_trial, expires_at)
        for subscription_id, is_trial, expires_at, _ in rows
        if expires_at is not None
    ]
    earliest = now - _LEARNED_GRACE
    learned_at = {
        subscription_id: min(now, max(synced_at, earliest))
        for subscription_id, _, _, synced_at in rows
        if synced_at is not None
    }
    await _plan(session, await schedules(session), subscriptions, now, learned_at)
    return len(subscriptions)


class ReplanArgs(BaseModel):
    """Страница подписок для пересчёта: после подписки `after_id`."""

    after_id: int = 0
    page_size: int = PLAN_BATCH


@task("reminders.replan", ReplanArgs, needs_attention=False)
async def replan_reminders(context: TaskContext, args: ReplanArgs) -> None:
    """Расписание изменили — напоминания всех подписок пересчитываются (7.3)."""
    session = context.session
    await _lock_planning(session)
    rows = (
        await session.execute(
            select(Subscription.id, Subscription.is_trial, Subscription.expires_at)
            .where(_plannable(), Subscription.id > args.after_id)
            .order_by(Subscription.id)
            .limit(args.page_size)
        )
    ).all()
    subscriptions = _with_end(rows)
    await _plan(session, await schedules(session), subscriptions, datetime.now(UTC))
    if len(rows) == args.page_size:
        await replan_reminders.enqueue(
            session, ReplanArgs(after_id=rows[-1][0], page_size=args.page_size)
        )


class ScheduleError(Exception):
    """Расписание не проходит проверку; текст показывается оператору."""


async def save_schedules(
    session: AsyncSession, paid: list[int], trial: list[int], *, member_id: int
) -> Schedules:
    """7.1–7.3: владелец задаёт расписания; изменение применяется без перезапуска, а
    запланированные напоминания пересчитываются. Каждое изменение — в журнал."""
    try:
        new = Schedules(
            paid=_SCHEDULE.validate_python(paid), trial=_SCHEDULE.validate_python(trial)
        )
    except ValidationError as error:
        message = str(error.errors()[0]["msg"]).removeprefix("Value error, ")
        raise ScheduleError(message) from error
    await _lock_planning(session)
    old = await schedules(session)
    if new == old:
        return new
    for setting, before, after in (
        (PAID_SCHEDULE, old.paid, new.paid),
        (TRIAL_SCHEDULE, old.trial, new.trial),
    ):
        if before != after:
            await set_setting(session, setting, after, member_id=member_id)
    # Убранные смещения не уходят уже сейчас; новые запланирует пересчёт
    await _cancel_outdated(session, new)
    await replan_reminders.enqueue(session, ReplanArgs())
    return new


# --- Отправка ---


def send_key(subscription_id: int) -> str:
    """Ключ отправки сообщений о сроке: одна на подписку за раз."""
    return f"subscription:{subscription_id}:reminders"


async def dispatch_due(session: AsyncSession, now: datetime) -> int:
    """Ставит отправку подписок, у которых наступили неотправленные сообщения о сроке.
    Возвращает, сколько отправок поставлено."""
    due = exists().where(
        Reminder.subscription_id == Subscription.id,
        Reminder.state == ReminderState.SCHEDULED,
        Reminder.due_at <= now,
    )
    # Отключённой в панели подписке напоминания не отправляются (7.16); после включения
    # уйдёт самое позднее из наступивших (7.19). Удалённую разбирает сама отправка
    query = select(Subscription.id).where(
        due,
        or_(
            Subscription.deleted_at.is_not(None),
            Subscription.panel_status.is_(None),
            Subscription.panel_status != PanelUserStatus.DISABLED,
        ),
    )
    state = await shop_state(session)
    if state != ShopState.OPEN:
        # Пока магазин не открыт или временно закрыт, сообщения о сроке получают только
        # команда и тестировщики (1.22, 7.17)
        privileged = exists().where(
            TelegramAccount.client_id == Subscription.client_id,
            TelegramAccount.telegram_id.in_(await team_and_testers(session)),
        )
        if state == ShopState.NOT_OPENED:
            # Остальным после открытия ничего не досылается: их предупреждал прежний бот
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.state == ReminderState.SCHEDULED,
                    Reminder.due_at <= now,
                    exists().where(Subscription.id == Reminder.subscription_id, ~privileged),
                )
                .values(state=ReminderState.SKIPPED)
            )
        # Временно закрытый: напоминания копятся, после открытия уйдёт самое позднее
        query = query.where(privileged)
    ids = list(await session.scalars(query.order_by(Subscription.id).limit(DISPATCH_BATCH)))
    keys = {send_key(subscription_id): subscription_id for subscription_id in ids}
    # Отправка, которая ждёт панель, не ставится второй раз каждую минуту
    waiting = await unfinished_keys(session, send_reminders.name, keys)
    queued = 0
    for key, subscription_id in keys.items():
        if key not in waiting:
            await send_reminders.enqueue(
                session, SendRemindersArgs(subscription_id=subscription_id), key=key
            )
            queued += 1
    return queued


class TickArgs(BaseModel):
    pass


@task("reminders.tick", TickArgs, needs_attention=False)
async def reminders_tick(context: TaskContext, _args: TickArgs) -> None:
    """Раз в минуту: запланировать новые даты окончания и отправить наступившее (0014).
    После недоступности магазина первый же проход досылает пропущенное (7.7)."""
    now = datetime.now(UTC)
    await plan_new(context.session, now)
    await dispatch_due(context.session, now)


REMINDERS_TICK = Periodic(reminders_tick, TickArgs(), TICK_EVERY)


class _Hold(enum.Enum):
    SEND = enum.auto()
    # Магазин временно закрыт: подождать открытия (1.22)
    WAIT = enum.auto()
    # Магазин не открыт: не отправлять и не досылать (7.17)
    SKIP = enum.auto()


async def _hold(session: AsyncSession, client_id: int) -> _Hold:
    state = await shop_state(session)
    if state == ShopState.OPEN:
        return _Hold.SEND
    telegram_id = await client_telegram_id(session, client_id)
    if telegram_id is not None and await is_team_or_tester(session, telegram_id):
        return _Hold.SEND
    return _Hold.SKIP if state == ShopState.NOT_OPENED else _Hold.WAIT


def _mark(reminders: Sequence[Reminder], state: ReminderState) -> None:
    for reminder in reminders:
        reminder.state = state


async def _reconcile_soon(session: AsyncSession, subscription_id: int) -> None:
    """Магазин знает о подписке не то, что панель, — сверка (7.6)."""
    key = operations_key(subscription_id)
    if not await unfinished_keys(session, reconcile_subscription.name, [key]):
        await enqueue_reconcile(session, subscription_id, Source.SYNC, "reminder")


@dataclass(frozen=True)
class _Message:
    key: str
    buttons: list[ButtonArgs]
    extra_keys: list[str]


async def _message(
    session: AsyncSession, subscription: Subscription, reminder: Reminder
) -> _Message:
    """С7–С9: текст по виду подписки и моменту; у триала — «Купить подписку» (7.13),
    у платной — «Продлить» этой подписки (7.5, 7.12)."""
    offset = reminder.offset_hours
    if subscription.is_trial:
        if offset > END_OFFSET:
            key = "reminder.trial_before"
        elif offset == END_OFFSET:
            key = "event.trial_ended"
        else:
            key = "reminder.trial_after"
        return _Message(key, [buy_button(), MAIN_MENU_BUTTON], [])
    extra: list[str] = []
    if offset > END_OFFSET:
        key = "reminder.before"
        tariff = (
            await session.get(Tariff, subscription.tariff_id)
            if subscription.tariff_id is not None
            else None
        )
        # Тариф в архиве: продлить его можно только пока подписка активна (7.11, 3.21)
        if tariff is not None and tariff.state == TariffState.ARCHIVED:
            extra.append("reminder.before.archived")
    elif offset == END_OFFSET:
        key = "event.expired"
    else:
        key = "reminder.after"
    return _Message(key, [renew_button(subscription.id), MAIN_MENU_BUTTON], extra)


async def _send(
    session: AsyncSession,
    subscription: Subscription,
    latest: Reminder,
    skipped: Sequence[Reminder],
    now: datetime,
) -> None:
    """Из наступивших отправляется только самое позднее, остальные пропускаются (7.6,
    7.7). Отметка «отправлено» и сообщение — в одной транзакции: повторно не уйдёт и
    после сбоя (7.8); недоставка — в журнал сообщений (7.14)."""
    _mark(skipped, ReminderState.SKIPPED)
    latest.state = ReminderState.SENT
    latest.sent_at = now
    message = await _message(session, subscription, latest)
    await send_to_client(
        session,
        subscription.client_id,
        message.key,
        variables={"subscription_name": subscription.name},
        dates={"end_date": latest.expires_at},
        buttons=message.buttons,
        extra_keys=message.extra_keys,
    )
    await record(
        session,
        actor=Actor.SYSTEM,
        action="reminder.sent",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details={
            "key": message.key,
            "offset_hours": latest.offset_hours,
            "expires_at": latest.expires_at.isoformat(),
            "skipped_offsets": [reminder.offset_hours for reminder in skipped],
        },
    )


class SendRemindersArgs(BaseModel):
    subscription_id: int


@task("reminders.send", SendRemindersArgs, needs_attention=False)
async def send_reminders(context: TaskContext, args: SendRemindersArgs) -> None:
    """Отправляет самое позднее из наступивших сообщений о сроке подписки, сверив дату
    окончания с панелью (7.6). Панель недоступна — задача ждёт её (4.30), а за это
    время могут наступить следующие: уйдёт только самое позднее."""
    session = context.session
    now = datetime.now(UTC)
    subscription = await session.get(Subscription, args.subscription_id, with_for_update=True)
    if subscription is None:
        return
    due = list(
        await session.scalars(
            select(Reminder)
            .where(
                Reminder.subscription_id == subscription.id,
                Reminder.state == ReminderState.SCHEDULED,
                Reminder.due_at <= now,
            )
            .order_by(Reminder.due_at, Reminder.id)
            .with_for_update()
        )
    )
    if not due:
        return
    if subscription.deleted_at is not None or ended_by_refund(subscription):
        # Клиент удалённую подписку не видит (4.6); закончил возврат — только
        # сообщение о возврате (10.13)
        _mark(due, ReminderState.CANCELLED)
        return
    hold = await _hold(session, subscription.client_id)
    if hold == _Hold.WAIT:
        return
    if hold == _Hold.SKIP:
        _mark(due, ReminderState.SKIPPED)
        return
    current = [reminder for reminder in due if reminder.expires_at == subscription.expires_at]
    # Дата окончания уже другая: проход планирования заменит их новыми (7.9)
    _mark([reminder for reminder in due if reminder not in current], ReminderState.CANCELLED)
    if not current:
        return
    user = await runtime.current().panel.get_user(subscription.panel_user_id)
    if user is None or user.expire_at != subscription.expires_at:
        # 7.6: дата в панели другая — напоминание не отправляется, расписание
        # пересчитывается от даты, которую принесёт сверка
        _mark(current, ReminderState.CANCELLED)
        await _reconcile_soon(session, subscription.id)
        return
    if user.status == UserStatus.DISABLED:
        # 7.16: отключённой подписке не отправляются; копятся до включения (7.19)
        if subscription.panel_status != PanelUserStatus.DISABLED:
            await _reconcile_soon(session, subscription.id)
        return
    await _send(session, subscription, current[-1], current[:-1], now)
