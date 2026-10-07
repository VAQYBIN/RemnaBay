"""Применение покупки и продления в панели и магазине (блок 3, решение 0057).

3.6, 3.11, 3.13, 3.14, 3.16–3.19, 3.22, 3.25, 3.37, 4.7, 4.15, 4.22, 4.34.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import PanelUserStatus, SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Subject, entries_for
from remnabay.messaging import RecipientBlockedError, SendArgs, send_message
from remnabay.panel import PanelUnavailableError
from remnabay.panel_names import PANEL_USERNAME_PREFIX, username_prefix
from remnabay.payments import (
    ApplyError,
    ShopApplier,
    TariffSnapshot,
    apply_as_new_subscription,
    apply_payment,
)
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask
from remnabay.shop_settings import ShopSettingValue
from tests.domain_support import add, make_client, make_payment, make_subscription, make_tariff
from tests.panel_support import FakePanel, FakeSender, fake_runtime

TELEGRAM_ID = 777
PAID_AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
SQUAD = UUID("6f0b2c3e-1d2a-4b5c-8d9e-0f1a2b3c4d5e")


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
async def client(db_session: AsyncSession) -> Client:
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
    return await make_client(db_session, telegram_id=TELEGRAM_ID)


@pytest.fixture
async def tariff(db_session: AsyncSession) -> Tariff:
    tariff = make_tariff(squad_uuids=[SQUAD], device_limit=3, duration_days=30)
    await add(db_session, tariff)
    return tariff


async def _paid(
    session: AsyncSession,
    client: Client,
    tariff: Tariff,
    *,
    purpose: PaymentPurpose = PaymentPurpose.PURCHASE,
    subscription: Subscription | None = None,
    operation_id: UUID | None = None,
    paid_at: datetime = PAID_AT,
) -> Payment:
    payment = make_payment(
        client,
        tariff,
        state=PaymentState.PAID,
        purpose=purpose,
        subscription_id=subscription.id if subscription else None,
        tariff_snapshot=TariffSnapshot.of(tariff).stored(),
        operation_id=operation_id or uuid4(),
        paid_at=paid_at,
    )
    await add(session, payment)
    return payment


async def _apply(session: AsyncSession, panel: FakePanel, payment: Payment) -> None:
    context = TaskContext(session=session, task_id=1, attempt_number=1)
    with fake_runtime(panel=panel, payments=ShopApplier(dev_mode=False)):
        await apply_payment.run(context, {"payment_id": payment.id})
    await session.flush()


async def _subscription_with_user(
    session: AsyncSession,
    panel: FakePanel,
    client: Client,
    tariff: Tariff | None,
    *,
    expires_at: datetime,
    is_trial: bool = False,
    **user: Any,
) -> Subscription:
    """Подписка, у которой срок по отрезкам и в панели совпадает."""
    panel.add_user(id=7, telegramId=TELEGRAM_ID, expireAt=expires_at.isoformat(), **user)
    subscription = make_subscription(client, tariff, panel_user_id=7)
    subscription.expires_at = expires_at
    subscription.is_trial = is_trial
    subscription.panel_short_uuid = "abc123"
    subscription.subscription_url = "https://sub.example.com/abc123"
    await add(session, subscription)
    kind = SegmentKind.TRIAL if is_trial else SegmentKind.GRANTED
    await add(
        session,
        TermSegment(
            subscription_id=subscription.id,
            kind=kind,
            starts_at=expires_at - timedelta(days=3),
            ends_at=expires_at,
        ),
    )
    return subscription


async def _messages(session: AsyncSession) -> list[SendArgs]:
    rows = await session.scalars(
        select(QueueTask.args).where(QueueTask.name == "messages.send").order_by(QueueTask.id)
    )
    return [SendArgs.model_validate(args) for args in rows]


async def _segments(session: AsyncSession, subscription_id: int) -> Sequence[TermSegment]:
    result = await session.scalars(
        select(TermSegment)
        .where(TermSegment.subscription_id == subscription_id)
        .order_by(TermSegment.starts_at)
    )
    return result.all()


def _writes(panel: FakePanel, method: str) -> list[dict[str, Any]]:
    return [body for name, body in panel.writes if name == method]


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --- Покупка ---


async def test_3_6_purchase_creates_exactly_one_panel_user_with_tariff_params(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.6, 3.37: оплата покупки — ровно один пользователь панели с параметрами тарифа
    (срок, лимит устройств, сквады), подписка связана с ним, платёж «применён»."""
    payment = await _paid(db_session, client, tariff)
    await _apply(db_session, panel, payment)

    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"rb_{TELEGRAM_ID}_1"
    assert _at(created["expireAt"]) == PAID_AT + timedelta(days=30)
    assert created["hwidDeviceLimit"] == 3
    assert created["activeInternalSquads"] == [str(SQUAD)]
    assert created["telegramId"] == TELEGRAM_ID
    assert (created["trafficLimitBytes"], created["trafficLimitStrategy"]) == (0, "NO_RESET")

    subscription = await db_session.scalar(select(Subscription))
    assert subscription is not None
    assert subscription.panel_username == f"rb_{TELEGRAM_ID}_1"
    assert subscription.tariff_id == tariff.id
    assert subscription.expires_at == PAID_AT + timedelta(days=30)
    assert payment.state == PaymentState.APPLIED
    assert payment.subscription_id == subscription.id


async def test_3_6_client_gets_subscription_link(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.6, решение 0057: клиент получает «Подписка готова» со ссылкой и кнопкой
    копирования (инструкция — этап 7)."""
    payment = await _paid(db_session, client, tariff)
    await _apply(db_session, panel, payment)

    [message] = await _messages(db_session)
    assert message.text_key == "event.subscription_ready"
    link = message.variables["subscription_link"]
    assert link.startswith("https://sub.example.com/")
    assert [(b.text_key, b.copy_text) for b in message.buttons] == [
        ("btn.copy_link", link),
        ("btn.main_menu", None),
    ]


async def test_3_14_first_subscription_gets_default_name(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.14: первая подписка — с названием по умолчанию («Основная»)."""
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    subscription = await db_session.scalar(select(Subscription))
    assert subscription is not None
    assert subscription.name == "Основная"


async def test_3_25_purchase_adds_segment_costing_paid_amount(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.25: покупка добавляет отрезок срока со стоимостью, равной оплаченной сумме."""
    payment = await _paid(db_session, client, tariff)
    await _apply(db_session, panel, payment)
    subscription = await db_session.scalar(select(Subscription))
    assert subscription is not None

    [segment] = await _segments(db_session, subscription.id)
    assert (segment.kind, segment.payment_id, segment.cost) == (
        SegmentKind.PAYMENT,
        payment.id,
        Decimal("199.00"),
    )
    assert (segment.starts_at, segment.ends_at) == (PAID_AT, PAID_AT + timedelta(days=30))


async def test_0057_prefix_is_locked_when_first_panel_user_is_created(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Решение 0057: префикс по умолчанию фиксируется, когда магазин создал первого
    пользователя в панели."""
    assert await username_prefix(db_session, dev_mode=False) == ("rb", False)
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    assert await username_prefix(db_session, dev_mode=False) == ("rb", True)


async def test_0057_operator_prefix_is_used(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Решение 0057: имя пользователя — <префикс оператора>_<Telegram ID>_<N>."""
    await db_session.merge(ShopSettingValue(key=PANEL_USERNAME_PREFIX.key, value="vpn"))
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"vpn_{TELEGRAM_ID}_1"


async def test_0057_dev_mode_uses_test_prefix(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Решение 0057: в режиме разработки префикс по умолчанию — rbtest."""
    payment = await _paid(db_session, client, tariff)
    context = TaskContext(session=db_session, task_id=1, attempt_number=1)
    with fake_runtime(panel=panel, payments=ShopApplier(dev_mode=True)):
        await apply_payment.run(context, {"payment_id": payment.id})
    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"rbtest_{TELEGRAM_ID}_1"


async def test_0057_name_taken_by_foreign_panel_user_takes_next_number(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Решение 0057, 4.15: имя занято пользователем, созданным в панели вручную, —
    магазин его не трогает и берёт следующий номер."""
    panel.add_user(id=50, username=f"rb_{TELEGRAM_ID}_1", telegramId=999)
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"rb_{TELEGRAM_ID}_2"


async def test_0057_second_subscription_gets_next_number(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Решение 0057: N — порядковый номер подписки клиента, созданной магазином."""
    first = make_subscription(client, tariff, panel_user_id=40)
    first.panel_username = f"rb_{TELEGRAM_ID}_1"
    first.deleted_at = PAID_AT
    await add(db_session, first)
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"rb_{TELEGRAM_ID}_2"


async def test_4_15_retry_after_lost_answer_does_not_create_second_user(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """4.15: панель создала пользователя, но ответ потерялся — повтор находит его и не
    создаёт второго."""
    payment = await _paid(db_session, client, tariff)
    panel.lose_next_write = True
    with pytest.raises(PanelUnavailableError):
        await _apply(db_session, panel, payment)
    await _apply(db_session, panel, payment)

    assert len(_writes(panel, "POST /api/users")) == 1
    assert len(panel.users) == 1
    assert payment.state == PaymentState.APPLIED


# --- Продление ---


async def test_3_17_active_subscription_extended_from_its_end_date(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.17, 3.19, 3.25: активная подписка — новая дата = прежняя дата + срок тарифа;
    пользователь и ссылка не меняются; отрезок со стоимостью оплаты."""
    end = PAID_AT + timedelta(days=5)
    subscription = await _subscription_with_user(db_session, panel, client, tariff, expires_at=end)
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    await _apply(db_session, panel, payment)

    assert _writes(panel, "POST /api/users") == []
    [update] = _writes(panel, "PATCH /api/users")
    assert _at(update["expireAt"]) == end + timedelta(days=30)
    assert subscription.expires_at == end + timedelta(days=30)
    assert subscription.subscription_url == "https://sub.example.com/abc123"
    assert subscription.panel_user_id == 7
    segments = await _segments(db_session, subscription.id)
    assert (segments[-1].starts_at, segments[-1].cost) == (end, Decimal("199.00"))
    [message] = await _messages(db_session)
    assert message.text_key == "event.renewed"


@pytest.mark.parametrize("ended", [timedelta(days=3), timedelta(seconds=1)])
async def test_3_18_3_22_expired_subscription_extended_from_payment_moment(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff, ended: timedelta
) -> None:
    """3.18, 3.22: истёкшая подписка (в том числе истёкшая, пока клиент платил) — новая
    дата = момент подтверждения оплаты + срок тарифа."""
    subscription = await _subscription_with_user(
        db_session, panel, client, tariff, expires_at=PAID_AT - ended, status="EXPIRED"
    )
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    await _apply(db_session, panel, payment)

    [update] = _writes(panel, "PATCH /api/users")
    assert _at(update["expireAt"]) == PAID_AT + timedelta(days=30)


async def test_3_37_renewal_writes_all_access_params_fixed_in_payment(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.37, 3.7: продление записывает в панель сквады, лимит устройств и лимит трафика
    тарифа на момент создания платежа; ручные правки в панели заменяются."""
    subscription = await _subscription_with_user(
        db_session,
        panel,
        client,
        tariff,
        expires_at=PAID_AT + timedelta(days=5),
        hwidDeviceLimit=10,
        trafficLimitBytes=5 * 1024**3,
        trafficLimitStrategy="MONTH",
    )
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    tariff.device_limit = 7
    await db_session.flush()
    await _apply(db_session, panel, payment)

    [update] = _writes(panel, "PATCH /api/users")
    assert update["hwidDeviceLimit"] == 3
    assert update["activeInternalSquads"] == [str(SQUAD)]
    assert update["trafficLimitBytes"] == 0
    assert update["trafficLimitStrategy"] == "NO_RESET"


async def test_3_37_reset_strategy_is_not_rewritten_when_unchanged(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.37: стратегия сброса записывается, только если отличается от текущей."""
    subscription = await _subscription_with_user(
        db_session, panel, client, tariff, expires_at=PAID_AT + timedelta(days=5)
    )
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    await _apply(db_session, panel, payment)
    [update] = _writes(panel, "PATCH /api/users")
    assert "trafficLimitStrategy" not in update


async def test_4_15_renewal_is_not_applied_twice_after_lost_answer(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """4.15, 4.34: панель продлила подписку, но ответ потерялся — повтор видит, что
    продление уже применено, и не продлевает второй раз; отрезок записан один."""
    end = PAID_AT + timedelta(days=5)
    subscription = await _subscription_with_user(db_session, panel, client, tariff, expires_at=end)
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    panel.lose_next_write = True
    with pytest.raises(PanelUnavailableError):
        await _apply(db_session, panel, payment)
    assert await _segments(db_session, subscription.id) != []
    await _apply(db_session, panel, payment)

    assert len(_writes(panel, "PATCH /api/users")) == 1
    assert subscription.expires_at == end + timedelta(days=30)
    paid_segments = [
        s for s in await _segments(db_session, subscription.id) if s.kind == SegmentKind.PAYMENT
    ]
    assert len(paid_segments) == 1


async def test_renewal_of_user_deleted_in_panel_fails_for_team(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """Сценарий продления: пользователя удалили в панели, пока клиент платил, — применить
    некуда; ошибка ведёт к повторам и затем к «оплачен — не применён» (4.20)."""
    subscription = make_subscription(client, tariff, panel_user_id=99)
    await add(db_session, subscription)
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    with pytest.raises(ApplyError):
        await _apply(db_session, panel, payment)


async def test_4_7_payment_for_disabled_subscription_extends_but_keeps_it_disabled(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """4.7: оплата отключённой в панели подписки применяется к сроку, пользователь
    остаётся отключённым, команда получает уведомление."""
    subscription = await _subscription_with_user(
        db_session,
        panel,
        client,
        tariff,
        expires_at=PAID_AT + timedelta(days=5),
        status="DISABLED",
    )
    payment = await _paid(
        db_session, client, tariff, purpose=PaymentPurpose.RENEWAL, subscription=subscription
    )
    await _apply(db_session, panel, payment)

    [update] = _writes(panel, "PATCH /api/users")
    assert "status" not in update
    assert subscription.panel_status == PanelUserStatus.DISABLED
    keys = [message.text_key for message in await _messages(db_session)]
    assert "team.paid_disabled_subscription" in keys


# --- Покупка поверх триала, двойная оплата, применение новой подпиской ---


async def test_3_16_purchase_applies_to_trial_subscription(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.16: покупка применяется к триальной подписке: пользователь и ссылка прежние,
    оплаченный срок добавляется к остатку триала, лимиты по тарифу, счётчик трафика
    обнулён."""
    trial_end = PAID_AT + timedelta(days=2)
    trial = await _subscription_with_user(
        db_session, panel, client, None, expires_at=trial_end, is_trial=True
    )
    payment = await _paid(db_session, client, tariff)
    await _apply(db_session, panel, payment)

    assert _writes(panel, "POST /api/users") == []
    [update] = _writes(panel, "PATCH /api/users")
    assert _at(update["expireAt"]) == trial_end + timedelta(days=30)
    assert update["hwidDeviceLimit"] == 3
    assert ("/api/users/7/actions/reset-traffic", {}) in panel.writes
    assert (trial.is_trial, trial.tariff_id) == (False, tariff.id)
    assert payment.subscription_id == trial.id
    assert trial.traffic_used_bytes == 0
    [message] = await _messages(db_session)
    assert message.text_key == "event.subscription_ready"


async def test_3_11_second_paid_invoice_of_one_purchase_extends_created_subscription(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.11: оплачены оба счёта одной покупки — первая оплата создаёт подписку, вторая
    продлевает её на срок своего тарифа; команда получает уведомление."""
    operation = uuid4()
    first = await _paid(db_session, client, tariff, operation_id=operation)
    second = await _paid(db_session, client, tariff, operation_id=operation)
    await _apply(db_session, panel, first)
    await _apply(db_session, panel, second)

    assert len(_writes(panel, "POST /api/users")) == 1
    [update] = _writes(panel, "PATCH /api/users")
    assert _at(update["expireAt"]) == PAID_AT + timedelta(days=60)
    assert second.subscription_id == first.subscription_id
    keys = [message.text_key for message in await _messages(db_session)]
    assert keys == ["event.subscription_ready", "team.double_payment", "event.renewed"]


async def test_4_22_apply_as_new_subscription_creates_new_one(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """4.22: «Применить созданием новой подписки» создаёт нового пользователя панели,
    даже если у клиента есть триальная подписка."""
    await _subscription_with_user(
        db_session, panel, client, None, expires_at=PAID_AT + timedelta(days=2), is_trial=True
    )
    member = await db_session.scalar(select(TeamMember))
    assert member is not None
    payment = await _paid(db_session, client, tariff)
    payment.state = PaymentState.PAID_NOT_APPLIED
    await apply_as_new_subscription(db_session, payment.id, member_id=member.id)
    await _apply(db_session, panel, payment)

    [created] = _writes(panel, "POST /api/users")
    assert created["username"] == f"rb_{TELEGRAM_ID}_1"
    assert payment.state == PaymentState.APPLIED


async def test_3_13_blocked_bot_does_not_stop_subscription(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """3.13: клиент заблокировал бот — подписка создана, недоставка записана в журнал."""
    payment = await _paid(db_session, client, tariff)
    await _apply(db_session, panel, payment)
    [message] = await _messages(db_session)

    sender = FakeSender(errors=[RecipientBlockedError("bot was blocked by the user")])
    context = TaskContext(session=db_session, task_id=2, attempt_number=1)
    with fake_runtime(sender=sender):
        await send_message.run(context, message.model_dump())

    assert payment.state == PaymentState.APPLIED
    actions = [
        entry.action for entry in await entries_for(db_session, Subject("client", client.id))
    ]
    assert "message.not_delivered" in actions


async def test_4_25_subscription_creation_is_journaled(
    db_session: AsyncSession, panel: FakePanel, client: Client, tariff: Tariff
) -> None:
    """4.25: создание подписки записано в журнал."""
    await _apply(db_session, panel, await _paid(db_session, client, tariff))
    subscription = await db_session.scalar(select(Subscription))
    assert subscription is not None
    [entry] = await entries_for(db_session, Subject("subscription", subscription.id))
    assert entry.action == "subscription.created"
