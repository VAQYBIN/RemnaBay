"""Возврат вручную и запросы на возврат (блок 10: 10.6, 10.8–10.16; решения 0033, 0065).

Также 4.15, 4.31 (смена даты при возврате) и 10.15 (привязка неизвестного платежа).
"""

from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import (
    Payment,
    PaymentPurpose,
    PaymentState,
    Refund,
    RefundRequest,
    RefundRequestState,
)
from remnabay.domain.reminders import Reminder, ReminderState
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Actor, ActorType, Subject, entries_for
from remnabay.messaging import SendArgs, send_message
from remnabay.panel import PanelUnavailableError
from remnabay.panel_sync import reconcile_subscription
from remnabay.payments import ShopApplier, TariffSnapshot, apply_payment, bind_unknown_payment
from remnabay.queue import (
    AttemptResult,
    QueueAttempt,
    TaskContext,
    TaskNotCancellableError,
    TaskStatus,
    cancel_failed,
    resolve_failed,
)
from remnabay.queue._models import QueueTask
from remnabay.refunds import (
    RefundError,
    RefundRequestError,
    change_term,
    cut_term,
    decline_request,
    file_request,
    refund_payment,
    refund_quote,
    unused_time,
    unused_value,
)
from remnabay.reminders import dispatch_due, plan_new
from remnabay.shop import SHOP_STATE, ShopState
from remnabay.worker.main import TASKS
from tests.domain_support import (
    add,
    make_client,
    make_payment,
    make_subscription,
    make_tariff,
    put_setting,
)
from tests.guide_support import queued, run_queued
from tests.panel_support import FakePanel, fake_runtime
from tests.web_support import Shop, running_shop

API = "/api/admin"
DAY = timedelta(days=1)
PRICE = Decimal("300.00")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _segment(
    start: datetime,
    end: datetime,
    *,
    payment_id: int | None = None,
    cost: Decimal = Decimal(0),
) -> TermSegment:
    return TermSegment(
        subscription_id=0,
        kind=SegmentKind.PAYMENT if payment_id else SegmentKind.GRANTED,
        payment_id=payment_id,
        starts_at=start,
        ends_at=end,
        cost=cost,
    )


# --- Расчёт по отрезкам (10.8, 10.9) ---


def test_10_8_unused_value_is_proportional_to_minutes_and_rounded_down() -> None:
    """10.8: неиспользованная стоимость — пропорционально оставшемуся времени отрезка с
    точностью до минуты, вниз до целой единицы валюты; будущий отрезок — целиком,
    использованный — ничего."""
    at = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    current = _segment(at - 10 * DAY, at + 20 * DAY, payment_id=1, cost=Decimal("199.00"))
    # 199 × 20/30 = 132,67 → 132
    assert unused_value([current], 1, at) == Decimal(132)
    # Неполная минута не считается: 199 × (20 дней − 1 минута)/30 дней → 132
    assert unused_value([current], 1, at + timedelta(seconds=59)) == Decimal(132)
    future = _segment(at + DAY, at + 31 * DAY, payment_id=1, cost=Decimal("199.00"))
    assert unused_value([future], 1, at) == Decimal(199)
    used = _segment(at - 30 * DAY, at - DAY, payment_id=1, cost=Decimal("199.00"))
    assert unused_value([used], 1, at) == Decimal(0)
    other = _segment(at - DAY, at + DAY, payment_id=2, cost=Decimal("199.00"))
    assert unused_value([other], 1, at) == Decimal(0)


async def test_10_9_cut_removes_future_trims_current_and_shifts_later(
    db_session: AsyncSession,
) -> None:
    """10.9: будущий отрезок платежа убирается целиком, текущий обрезается по момент
    возврата, использованный не меняется; отрезки после него сдвигаются."""
    client = await make_client(db_session)
    subscription = make_subscription(client, None)
    await add(db_session, subscription)
    payment = make_payment(client, None, state=PaymentState.APPLIED)
    await add(db_session, payment)
    at = _now()
    used = _segment(at - 40 * DAY, at - 10 * DAY, payment_id=payment.id, cost=PRICE)
    current = _segment(at - 10 * DAY, at + 20 * DAY, payment_id=payment.id, cost=PRICE)
    gift = _segment(at + 20 * DAY, at + 25 * DAY)
    for segment in (used, current, gift):
        segment.subscription_id = subscription.id
    await add(db_session, used, current, gift)

    assert unused_time([used, current, gift], payment.id, at) == 20 * DAY
    removed = await cut_term(db_session, [used, current, gift], payment.id, at)

    assert removed == 20 * DAY
    assert (used.starts_at, used.ends_at) == (at - 40 * DAY, at - 10 * DAY)
    assert (current.starts_at, current.ends_at) == (at - 10 * DAY, at)
    assert (gift.starts_at, gift.ends_at) == (at, at + 5 * DAY)


# --- Мир для сценариев возврата ---


class World:
    """Клиент с подпиской: отрезки срока и тот же срок в подставной панели."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.panel = FakePanel()
        self.client: Client
        self.owner: TeamMember
        self.tariff: Tariff
        self.subscription: Subscription

    async def setup(self) -> None:
        await put_setting(self.session, SHOP_STATE, ShopState.OPEN)
        self.owner = TeamMember(telegram_id=1, role=TeamRole.OWNER)
        await add(self.session, self.owner)
        self.client = await make_client(self.session, telegram_id=777)
        self.tariff = make_tariff(price=PRICE, duration_days=30)
        await add(self.session, self.tariff)
        self.subscription = make_subscription(self.client, self.tariff, panel_user_id=7)
        await add(self.session, self.subscription)
        self.panel.add_user(
            id=7, shortUuid="short7", subscriptionUrl="https://sub.example.com/short7"
        )

    async def paid(
        self, start: datetime, days: int = 30, *, amount: Decimal = PRICE, **overrides: Any
    ) -> Payment:
        """Применённый платёж и его отрезок; срок в панели — до конца последнего отрезка."""
        values: dict[str, Any] = {
            "state": PaymentState.APPLIED,
            "purpose": PaymentPurpose.RENEWAL,
            "subscription_id": self.subscription.id,
            "tariff_snapshot": TariffSnapshot.of(self.tariff).stored(),
            "amount": amount,
            "paid_amount": amount,
            "paid_currency": "RUB",
            "paid_at": start,
            "applied_at": start,
        }
        values.update(overrides)
        payment = make_payment(self.client, self.tariff, **values)
        await add(self.session, payment)
        await add(
            self.session,
            TermSegment(
                subscription_id=self.subscription.id,
                kind=SegmentKind.PAYMENT,
                payment_id=payment.id,
                starts_at=start,
                ends_at=start + days * DAY,
                cost=amount,
            ),
        )
        await self.set_end(start + days * DAY)
        return payment

    async def set_end(self, end: datetime) -> None:
        self.subscription.expires_at = end
        self.panel.users[7]["expireAt"] = end.isoformat()
        await self.session.flush()

    async def run_term_changes(self) -> int:
        with fake_runtime(panel=self.panel):
            return await run_queued(self.session, change_term)

    async def segments(self) -> Sequence[TermSegment]:
        rows = await self.session.scalars(
            select(TermSegment)
            .where(TermSegment.subscription_id == self.subscription.id)
            .order_by(TermSegment.starts_at)
            .execution_options(populate_existing=True)
        )
        return rows.all()

    async def messages(self) -> list[SendArgs]:
        return [
            SendArgs.model_validate(task.args) for task in await queued(self.session, send_message)
        ]

    def owner_actor(self) -> Actor:
        return Actor.team_member(self.owner.id)

    def panel_end(self) -> datetime:
        return datetime.fromisoformat(str(self.panel.users[7]["expireAt"]))


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    world = World(db_session)
    await world.setup()
    return world


# --- Предложенная сумма и экран подтверждения (10.8, 10.10) ---


async def test_10_8_10_10_quote_suggests_unused_value_and_shows_term_ending_now(
    world: World,
) -> None:
    """10.8, 10.10: система предлагает неиспользованную стоимость платежа; экран
    подтверждения показывает, что подписка закончится сейчас."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)

    quote = await refund_quote(world.session, payment.id, now)

    assert (quote.paid, quote.suggested) == (PRICE, Decimal(200))
    assert quote.change is not None
    assert quote.change.ends_now is True
    assert quote.change.new_end == now


async def test_10_10_quote_shows_new_end_when_term_continues(world: World) -> None:
    """10.10: возвращаемый платёж — будущий отрезок: показывается новая дата окончания."""
    now = _now()
    await world.paid(now - 10 * DAY)
    future = await world.paid(now + 20 * DAY)

    quote = await refund_quote(world.session, future.id, now)

    assert quote.suggested == PRICE
    assert quote.change is not None
    assert (quote.change.new_end, quote.change.ends_now) == (now + 20 * DAY, False)


# --- Возврат и смена срока (10.9, 10.11, 10.12) ---


async def test_10_9_10_11_refund_shortens_term_and_notifies_after_panel(world: World) -> None:
    """10.9, 10.11: платёж сразу «частично возвращён», возврат выполнен вручную и записан
    в журнал; срок сокращается на неиспользованную часть независимо от суммы; клиент
    получает сообщение, когда дата применена в панели."""
    now = _now()
    await world.paid(now - 10 * DAY)
    future = await world.paid(now + 20 * DAY)
    await add(
        world.session,
        TermSegment(
            subscription_id=world.subscription.id,
            kind=SegmentKind.GRANTED,
            starts_at=now + 50 * DAY,
            ends_at=now + 55 * DAY,
        ),
    )
    await world.set_end(now + 55 * DAY)

    await refund_payment(
        world.session, future.id, amount=Decimal(100), member_id=world.owner.id, now=now
    )

    assert future.state == PaymentState.PARTIALLY_REFUNDED
    refund = await world.session.scalar(select(Refund).where(Refund.payment_id == future.id))
    assert refund is not None
    assert (refund.amount, refund.performed_manually, refund.performed_by_id) == (
        Decimal(100),
        True,
        world.owner.id,
    )
    [entry] = [
        e
        for e in await entries_for(world.session, Subject("payment", future.id))
        if e.action == "payment.refunded"
    ]
    assert (entry.actor_type, entry.actor_ref) == (ActorType.TEAM_MEMBER, str(world.owner.id))
    # Срок ещё не применён в панели — сообщения нет
    assert await world.messages() == []

    assert await world.run_term_changes() == 1

    assert world.panel_end() == now + 25 * DAY
    assert world.subscription.expires_at == now + 25 * DAY
    assert [(s.kind, s.starts_at, s.ends_at) for s in await world.segments()] == [
        (SegmentKind.PAYMENT, now - 10 * DAY, now + 20 * DAY),
        (SegmentKind.GRANTED, now + 20 * DAY, now + 25 * DAY),
    ]
    [message] = await world.messages()
    assert message.text_key == "event.refunded"
    assert message.variables["subscription_name"] == "Основная"
    assert set(message.dates) == {"payment_date", "end_date"}
    assert message.dates["end_date"] == now + 25 * DAY


async def test_10_11_full_amount_makes_payment_refunded(world: World) -> None:
    """10.11, 10.12: возврат всей суммы — «возвращён»."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)
    await refund_payment(world.session, payment.id, amount=PRICE, member_id=world.owner.id, now=now)
    assert payment.state == PaymentState.REFUNDED


async def test_10_12_one_refund_per_payment_and_not_more_than_paid(world: World) -> None:
    """10.8, 10.12: нельзя вернуть больше заплаченного; по платежу возможен один возврат."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)
    with pytest.raises(RefundError, match="больше"):
        await refund_payment(
            world.session, payment.id, amount=PRICE + 1, member_id=world.owner.id, now=now
        )
    await refund_payment(
        world.session, payment.id, amount=Decimal(1), member_id=world.owner.id, now=now
    )
    with pytest.raises(RefundError, match="повторный"):
        await refund_payment(
            world.session, payment.id, amount=Decimal(1), member_id=world.owner.id, now=now
        )
    with pytest.raises(RefundError, match="повторный"):
        await refund_quote(world.session, payment.id, now)


async def test_10_9_fully_used_payment_keeps_term_and_notifies_at_once(world: World) -> None:
    """10.9: полностью использованный отрезок не меняется — срок прежний, сообщение сразу."""
    now = _now()
    used = await world.paid(now - 40 * DAY)
    await world.paid(now - 10 * DAY)

    quote = await refund_quote(world.session, used.id, now)
    assert (quote.suggested, quote.change) == (Decimal(0), None)
    await refund_payment(
        world.session, used.id, amount=Decimal(50), member_id=world.owner.id, now=now
    )

    assert await queued(world.session, change_term) == []
    [message] = await world.messages()
    assert (message.text_key, message.dates["end_date"]) == ("event.refunded", now + 20 * DAY)


# --- Подписка заканчивается сейчас (10.13) ---


async def test_10_13_refund_ending_subscription_silences_reminders(world: World) -> None:
    """10.13: подписка, которую возврат закончил сейчас, не получает напоминаний и
    «Подписка истекла» — только сообщение о возврате с текстом о завершении. После
    продления сообщения о сроке снова идут."""
    now = _now()
    payment = await world.paid(now - 28 * DAY)
    await plan_new(world.session, now - 60 * DAY)

    await refund_payment(
        world.session, payment.id, amount=Decimal(10), member_id=world.owner.id, now=now
    )
    await world.run_term_changes()

    assert world.subscription.expires_at == now
    [message] = await world.messages()
    assert message.text_key == "event.refunded.ended"
    states = set(
        await world.session.scalars(
            select(Reminder.state)
            .where(Reminder.subscription_id == world.subscription.id)
            .execution_options(populate_existing=True)
        )
    )
    assert states == {ReminderState.CANCELLED}

    # Проходы планировщика после окончания: ни «Подписка истекла», ни «через 3 дня»
    for moment in (now + timedelta(minutes=1), now + 4 * DAY):
        await plan_new(world.session, moment)
        await dispatch_due(world.session, moment)
    assert (
        await world.session.scalar(
            select(Reminder.id).where(
                Reminder.subscription_id == world.subscription.id,
                Reminder.expires_at == world.subscription.expires_at,
            )
        )
        is None
    )
    assert len(await world.messages()) == 1

    # Продлили — новый срок планируется как обычно
    await world.set_end(now + 30 * DAY)
    await plan_new(world.session, now + timedelta(minutes=2))
    assert (
        await world.session.scalar(
            select(Reminder.id).where(
                Reminder.subscription_id == world.subscription.id,
                Reminder.expires_at == now + 30 * DAY,
            )
        )
        is not None
    )


# --- Повтор и разбор смены даты (4.15, 4.31) ---


async def test_4_15_term_change_is_not_applied_twice_after_lost_answer(world: World) -> None:
    """4.15: прошлая попытка сократила срок в панели, а ответ потерялся — повтор не
    сокращает его второй раз, а только записывает отрезки."""
    now = _now()
    await world.paid(now - 10 * DAY)
    future = await world.paid(now + 20 * DAY)
    await refund_payment(world.session, future.id, amount=PRICE, member_id=world.owner.id, now=now)
    [task] = await queued(world.session, change_term)

    world.panel.lose_next_write = True
    context = TaskContext(session=world.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=world.panel), pytest.raises(PanelUnavailableError):
        async with world.session.begin_nested():
            await change_term.run(context, task.args)
    assert world.panel_end() == now + 20 * DAY

    await world.run_term_changes()

    patches = [body for name, body in world.panel.writes if name == "PATCH /api/users"]
    assert len(patches) == 1
    assert [s.ends_at for s in await world.segments()] == [now + 20 * DAY]


async def _failed(world: World, task: QueueTask) -> None:
    task.status = TaskStatus.FAILED
    await add(
        world.session,
        QueueAttempt(
            task_id=task.id,
            number=1,
            retry_round=1,
            finished_at=_now(),
            result=AttemptResult.ERROR,
            error="Панель ответила 500",
        ),
    )


async def test_4_31_term_change_cannot_be_cancelled_and_resolution_notifies(
    world: World,
) -> None:
    """4.31, 10.11: смену даты при возврате нельзя отменить — деньги уже вернули; если
    команда отметила её решённой вручную, клиент получает сообщение о возврате, а
    магазин сверяет подписку с панелью."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)
    await refund_payment(world.session, payment.id, amount=PRICE, member_id=world.owner.id, now=now)
    [task] = await queued(world.session, change_term)
    await _failed(world, task)

    with pytest.raises(TaskNotCancellableError):
        await cancel_failed(world.session, TASKS, task.id, actor=world.owner_actor(), comment="нет")
    await resolve_failed(
        world.session, TASKS, task.id, actor=world.owner_actor(), comment="Сменил дату в панели"
    )

    [message] = await world.messages()
    assert message.text_key == "event.refunded.ended"
    assert world.subscription.ended_by_refund_at is not None
    assert await queued(world.session, reconcile_subscription)


# --- Неприменённый и неизвестный платёж (10.14), «решён вручную» (10.16) ---


async def test_10_14_unapplied_payment_refund_unblocks_subscription_operations(
    world: World,
) -> None:
    """10.14: у неприменённого платежа сумма по умолчанию — вся оплата; возврат закрывает
    ожидающее применение и снимает ожидание с операций подписки (4.27); клиент получает
    «Мы разобрались с вашим платежом: деньги возвращены»."""
    now = _now()
    payment = make_payment(
        world.client,
        world.tariff,
        state=PaymentState.PAID_NOT_APPLIED,
        purpose=PaymentPurpose.RENEWAL,
        subscription_id=world.subscription.id,
        tariff_snapshot=TariffSnapshot.of(world.tariff).stored(),
        amount=PRICE,
        paid_amount=Decimal("250.00"),
        paid_currency="RUB",
        paid_at=now,
    )
    await add(world.session, payment)
    key = f"subscription:{world.subscription.id}"
    apply_task = QueueTask(
        name=apply_payment.name, args={"payment_id": payment.id}, key=key, status=TaskStatus.FAILED
    )
    behind = QueueTask(name="subscriptions.grant_days", args={}, key=key)
    await add(world.session, apply_task, behind)

    quote = await refund_quote(world.session, payment.id, now)
    # Провайдер подтвердил 250 вместо 300: «вся оплата» — подтверждённая сумма
    assert (quote.paid, quote.suggested, quote.change) == (Decimal(250), Decimal(250), None)

    await refund_payment(
        world.session, payment.id, amount=Decimal(250), member_id=world.owner.id, now=now
    )

    assert payment.state == PaymentState.REFUNDED
    await world.session.refresh(apply_task)
    assert apply_task.status == TaskStatus.RESOLVED
    [message] = await world.messages()
    assert message.text_key == "event.payment_resolved.refunded"


async def test_10_14_unknown_payment_can_be_refunded_in_full(world: World) -> None:
    """10.14: неизвестный платёж — сумма по умолчанию вся оплата; клиента нет — некому
    писать."""
    payment = make_payment(
        None,
        None,
        is_unknown=True,
        state=PaymentState.PAID_NOT_APPLIED,
        tariff_snapshot=None,
        purpose=None,
        amount=Decimal("199.00"),
        paid_amount=Decimal("199.00"),
        paid_currency="RUB",
        paid_at=_now(),
    )
    await add(world.session, payment)

    quote = await refund_quote(world.session, payment.id, _now())
    assert quote.suggested == Decimal("199.00")
    await refund_payment(
        world.session, payment.id, amount=Decimal("199.00"), member_id=world.owner.id, now=_now()
    )
    assert payment.state == PaymentState.REFUNDED
    assert await world.messages() == []


async def test_10_16_resolved_manually_full_amount_and_term_unchanged(world: World) -> None:
    """10.16: у платежа «решён вручную» сумма по умолчанию — вся оплата, срок подписки
    магазин не меняет: применяла его команда."""
    now = _now()
    await world.paid(now - 10 * DAY)
    manual = make_payment(
        world.client,
        world.tariff,
        state=PaymentState.RESOLVED_MANUALLY,
        purpose=PaymentPurpose.RENEWAL,
        subscription_id=world.subscription.id,
        paid_amount=Decimal("199.00"),
        paid_currency="RUB",
        amount=Decimal("199.00"),
        paid_at=now - DAY,
    )
    await add(world.session, manual)

    quote = await refund_quote(world.session, manual.id, now)
    assert (quote.suggested, quote.change) == (Decimal("199.00"), None)
    await refund_payment(
        world.session, manual.id, amount=Decimal("199.00"), member_id=world.owner.id, now=now
    )

    assert await queued(world.session, change_term) == []
    assert world.panel.writes == []
    [message] = await world.messages()
    assert (message.text_key, message.dates["end_date"]) == ("event.refunded", now + 20 * DAY)


@pytest.mark.parametrize(
    "state", [PaymentState.PENDING, PaymentState.PAID, PaymentState.EXPIRED, PaymentState.DECLINED]
)
async def test_10_16_only_listed_states_are_refundable(world: World, state: PaymentState) -> None:
    """10.16: вернуть можно «применён», «оплачен — не применён», неизвестный и «решён
    вручную»."""
    payment = make_payment(world.client, world.tariff, state=state)
    await add(world.session, payment)
    with pytest.raises(RefundError):
        await refund_quote(world.session, payment.id, _now())


async def test_10_16_zero_amount_payment_is_not_refundable(world: World) -> None:
    """10.16: платёж с нулевой суммой (скидка 100%) вернуть нельзя — возвращать нечего."""
    payment = make_payment(
        world.client,
        world.tariff,
        state=PaymentState.APPLIED,
        amount=Decimal(0),
        provider=None,
        provider_payment_id=None,
    )
    await add(world.session, payment)
    with pytest.raises(RefundError, match="нулевой"):
        await refund_quote(world.session, payment.id, _now())


# --- Запросы на возврат (10.6) ---


async def test_10_6_refund_closes_the_only_open_request(world: World) -> None:
    """10.6: возврат закрывает единственный открытый запрос клиента сам."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)
    request = await file_request(
        world.session, world.client.id, comment="Не работает", member_id=world.owner.id
    )

    refund = await refund_payment(
        world.session, payment.id, amount=PRICE, member_id=world.owner.id, now=now
    )

    assert (request.state, request.refund_id, request.closed_by_id) == (
        RefundRequestState.DONE,
        refund.id,
        world.owner.id,
    )


async def test_10_6_several_open_requests_owner_chooses(world: World) -> None:
    """10.6: из нескольких открытых запросов возврат закрывает выбранный владельцем."""
    now = _now()
    payment = await world.paid(now - 10 * DAY)
    first, second = [
        await file_request(
            world.session, world.client.id, comment=f"Запрос {n}", member_id=world.owner.id
        )
        for n in (1, 2)
    ]
    with pytest.raises(RefundRequestError, match="выберите"):
        await refund_payment(
            world.session, payment.id, amount=PRICE, member_id=world.owner.id, now=now
        )

    await refund_payment(
        world.session,
        payment.id,
        amount=PRICE,
        member_id=world.owner.id,
        now=now,
        request_id=second.id,
    )
    assert (first.state, second.state) == (RefundRequestState.OPEN, RefundRequestState.DONE)


async def test_10_6_declined_request_notifies_client(world: World) -> None:
    """10.6: отказ — с комментарием; клиент получает «В возврате отказано», комментарий
    виден только команде."""
    request = await file_request(
        world.session, world.client.id, comment="Хочу деньги", member_id=world.owner.id
    )
    with pytest.raises(RefundRequestError, match="комментарий"):
        await decline_request(
            world.session, world.client.id, request.id, comment=" ", member_id=1, now=_now()
        )
    await decline_request(
        world.session,
        world.client.id,
        request.id,
        comment="Пользовался весь месяц",
        member_id=world.owner.id,
        now=_now(),
    )

    assert (request.state, request.close_comment) == (
        RefundRequestState.REJECTED,
        "Пользовался весь месяц",
    )
    [message] = await world.messages()
    assert message.text_key == "event.refund_declined"
    assert "Пользовался" not in str(message.variables)
    with pytest.raises(RefundRequestError, match="закрыт"):
        await decline_request(
            world.session, world.client.id, request.id, comment="ещё", member_id=1, now=_now()
        )


# --- Привязка неизвестного платежа (10.15) ---


async def test_10_15_bound_unknown_payment_segment_costs_actual_amount(world: World) -> None:
    """10.15: команда выбирает клиента, подписку и тариф; стоимость отрезка равна
    фактически пришедшей сумме, а не цене тарифа."""
    now = _now()
    await world.paid(now - 10 * DAY)
    unknown = make_payment(
        None,
        None,
        is_unknown=True,
        state=PaymentState.PAID_NOT_APPLIED,
        tariff_snapshot=None,
        purpose=None,
        amount=Decimal("150.00"),
        paid_amount=Decimal("150.00"),
        paid_currency="RUB",
        paid_at=now,
    )
    await add(world.session, unknown)

    await bind_unknown_payment(
        world.session,
        unknown.id,
        member_id=world.owner.id,
        client_id=world.client.id,
        purpose=PaymentPurpose.RENEWAL,
        subscription_id=world.subscription.id,
        tariff_id=world.tariff.id,
        tariff_snapshot=TariffSnapshot.of(world.tariff).stored(),
    )
    context = TaskContext(session=world.session, task_id=1, attempt_number=1)
    with fake_runtime(panel=world.panel, payments=ShopApplier(dev_mode=False)):
        await apply_payment.run(context, {"payment_id": unknown.id})

    assert unknown.state == PaymentState.APPLIED
    segment = await world.session.scalar(
        select(TermSegment).where(TermSegment.payment_id == unknown.id)
    )
    assert segment is not None
    assert (segment.cost, segment.ends_at - segment.starts_at) == (Decimal("150.00"), 30 * DAY)
    assert world.tariff.price == PRICE


# --- API: права и экран (А5, 10.8, 10.10) ---


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


async def _shop_world(shop: Shop) -> World:
    world = World(shop.session)
    world.panel = shop.panel
    await world.setup()
    return world


async def test_10_8_api_refund_is_owner_only(shop: Shop) -> None:
    """01-domain, таблица ролей: вернуть деньги может только владелец — помощник не
    видит «Вернуть» и получает отказ."""
    world = await _shop_world(shop)
    payment = await world.paid(_now() - 10 * DAY)
    assistant = TeamMember(telegram_id=2, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)

    await shop.sign_in(assistant)
    card = (await shop.http.get(f"{API}/payments/{payment.id}")).json()
    assert "refund" not in card["actions"]
    assert (await shop.http.get(f"{API}/payments/{payment.id}/refund")).status_code == 403
    response = await shop.http.post(f"{API}/payments/{payment.id}/refund", json={"amount": "1"})
    assert response.status_code == 403

    await shop.sign_in(world.owner)
    card = (await shop.http.get(f"{API}/payments/{payment.id}")).json()
    assert "refund" in card["actions"]


async def test_10_10_api_confirmation_screen_and_refund(shop: Shop) -> None:
    """10.8, 10.10–10.12: экран возврата — сумма, предел, новая дата окончания и открытые
    запросы; после возврата «Вернуть» больше нет, в карточке — выполненный возврат."""
    world = await _shop_world(shop)
    now = _now()
    # Час запаса: пока идёт запрос, неиспользованная стоимость не опускается ниже 200
    payment = await world.paid(now - 10 * DAY + timedelta(hours=1))
    request = await file_request(
        shop.session, world.client.id, comment="Просит", member_id=world.owner.id
    )
    await shop.sign_in(world.owner)

    quote = (await shop.http.get(f"{API}/payments/{payment.id}/refund")).json()
    assert (Decimal(quote["paid"]), Decimal(quote["suggested"])) == (PRICE, Decimal(200))
    assert quote["ends_now"] is True
    assert [r["id"] for r in quote["open_requests"]] == [request.id]

    response = await shop.http.post(
        f"{API}/payments/{payment.id}/refund", json={"amount": "350.00"}
    )
    assert response.status_code == 409
    response = await shop.http.post(
        f"{API}/payments/{payment.id}/refund", json={"amount": "200.00"}
    )
    assert response.status_code == 200
    card = response.json()
    assert card["payment"]["state"] == "partially_refunded"
    assert "refund" not in card["actions"]
    assert Decimal(card["refund"]["amount"]) == Decimal(200)
    assert card["refund"]["manually"] is True
    closed = await shop.session.get(RefundRequest, request.id)
    assert closed is not None
    assert closed.state == RefundRequestState.DONE
