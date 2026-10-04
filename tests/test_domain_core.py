"""Модель данных, часть 1: ядро коммерции (0015).

Проверяются правила спеки, которые держит сама база: роль участника команды (1.3),
тип тарифа и его параметры (2.3, 0009), запрет удаления тарифа с подписками (2.8),
отрезки срока (3.25, 3.26), один возврат на платёж (10.12) и другие.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.db import Base
from remnabay.domain.bonus import BonusOperation, BonusReason
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import (
    Payment,
    PaymentPurpose,
    PaymentState,
    Refund,
    RefundMethod,
    RefundRequest,
    RefundRequestState,
)
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment, TrafficSegment
from remnabay.domain.tariffs import Tariff, TariffType, TrafficResetStrategy
from remnabay.domain.team import TeamMember, TeamRole

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
GB = 1024**3


async def _add(session: AsyncSession, *objects: Base) -> None:
    session.add_all(objects)
    await session.flush()


async def _add_in_savepoint(session: AsyncSession, *objects: Base) -> None:
    async with session.begin_nested():
        session.add_all(objects)
        await session.flush()


async def _assert_rejected(session: AsyncSession, *objects: Base) -> None:
    """База отклоняет запись; сессия остаётся пригодной для следующих шагов теста."""
    with pytest.raises(IntegrityError):
        await _add_in_savepoint(session, *objects)


def _tariff(**overrides: object) -> Tariff:
    values: dict[str, object] = {
        "name": "Месяц",
        "type": TariffType.TERM_UNLIMITED,
        "duration_days": 30,
        "price": Decimal("199.00"),
        "device_limit": 3,
        "squad_uuids": [uuid4()],
    }
    values.update(overrides)
    return Tariff(**values)


async def _client(session: AsyncSession, telegram_id: int = 100) -> Client:
    client = Client()
    await _add(session, client)
    await _add(session, TelegramAccount(telegram_id=telegram_id, client_id=client.id))
    return client


def _subscription(client: Client, tariff: Tariff | None, panel_user_id: int = 1) -> Subscription:
    return Subscription(
        client_id=client.id,
        name="Основная",
        tariff_id=tariff.id if tariff else None,
        panel_user_id=panel_user_id,
        panel_username=f"user_{panel_user_id}",
        panel_short_uuid=f"short{panel_user_id}",
        subscription_url=f"https://sub.example.com/short{panel_user_id}",
    )


def _payment(client: Client | None, tariff: Tariff | None, **overrides: object) -> Payment:
    values: dict[str, object] = {
        "client_id": client.id if client else None,
        "purpose": PaymentPurpose.PURCHASE,
        "state": PaymentState.PENDING,
        "tariff_id": tariff.id if tariff else None,
        "tariff_snapshot": {"price": "199.00", "duration_days": 30},
        "amount": Decimal("199.00"),
        "currency": "RUB",
        "provider": "yookassa",
        "provider_payment_id": str(uuid4()),
    }
    values.update(overrides)
    return Payment(**values)


# --- Команда ---


async def test_1_3_team_member_stores_role(db_session: AsyncSession) -> None:
    """1.3: модель команды хранит роль участника — владелец или помощник."""
    owner = TeamMember(telegram_id=100500, role=TeamRole.OWNER)
    assistant = TeamMember(telegram_id=100501, role=TeamRole.ASSISTANT)
    await _add(db_session, owner, assistant)

    roles = await db_session.scalars(select(TeamMember.role).order_by(TeamMember.id))
    assert list(roles) == [TeamRole.OWNER, TeamRole.ASSISTANT]


async def test_one_telegram_account_is_one_active_team_member(db_session: AsyncSession) -> None:
    """Один Telegram-аккаунт — один действующий участник; после отзыва доступа его
    можно добавить снова, а прежняя строка остаётся для журнала."""
    first = TeamMember(telegram_id=100500, role=TeamRole.ASSISTANT)
    await _add(db_session, first)

    await _assert_rejected(db_session, TeamMember(telegram_id=100500, role=TeamRole.OWNER))

    first.revoked_at = NOW
    await _add(db_session, TeamMember(telegram_id=100500, role=TeamRole.OWNER))


# --- Клиент ---


async def test_one_telegram_account_belongs_to_one_client(db_session: AsyncSession) -> None:
    """Один Telegram-аккаунт — один клиент: по нему магазин узнаёт клиента."""
    await _client(db_session, telegram_id=42)
    other = Client()
    await _add(db_session, other)

    await _assert_rejected(db_session, TelegramAccount(telegram_id=42, client_id=other.id))


# --- Тарифы ---


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": TariffType.TERM_UNLIMITED},
        {
            "type": TariffType.TERM_QUOTA,
            "traffic_limit_bytes": 100 * GB,
            "traffic_reset_strategy": TrafficResetStrategy.MONTH_FROM_CREATION,
        },
        {"type": TariffType.TERM_PACKAGE, "traffic_limit_bytes": 50 * GB},
        {"type": TariffType.PACKAGE_ONLY, "duration_days": None, "traffic_limit_bytes": 50 * GB},
    ],
    ids=["term_unlimited", "term_quota", "term_package", "package_only"],
)
async def test_2_3_tariff_stores_one_of_four_types(
    db_session: AsyncSession, overrides: dict[str, object]
) -> None:
    """2.3: модель тарифа хранит тип; каждый из четырёх типов — со своими параметрами (0009)."""
    tariff = _tariff(**overrides)
    await _add(db_session, tariff)

    assert (
        await db_session.scalar(select(Tariff.type).where(Tariff.id == tariff.id))
        == (overrides["type"])
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": TariffType.TERM_UNLIMITED, "traffic_limit_bytes": 100 * GB},
        {"type": TariffType.TERM_QUOTA, "traffic_limit_bytes": 100 * GB},
        {"type": TariffType.TERM_PACKAGE},
        {
            "type": TariffType.TERM_PACKAGE,
            "traffic_limit_bytes": 50 * GB,
            "traffic_reset_strategy": TrafficResetStrategy.MONTH,
        },
        {"type": TariffType.PACKAGE_ONLY, "traffic_limit_bytes": 50 * GB},
        {"type": TariffType.TERM_UNLIMITED, "duration_days": None},
        {"duration_days": 0},
        {"price": Decimal("-1")},
        {"name": ""},
    ],
    ids=[
        "unlimited_with_traffic_limit",
        "quota_without_reset_strategy",
        "package_without_size",
        "package_with_reset_strategy",
        "package_only_with_duration",
        "term_without_duration",
        "zero_duration",
        "negative_price",
        "empty_name",
    ],
)
async def test_2_3_free_combination_of_tariff_parameters_is_rejected(
    db_session: AsyncSession, overrides: dict[str, object]
) -> None:
    """0009: свободная комбинация параметров тарифа не допускается."""
    await _assert_rejected(db_session, _tariff(**overrides))


async def _delete_tariff_in_savepoint(session: AsyncSession, tariff_id: int) -> None:
    async with session.begin_nested():
        await session.execute(delete(Tariff).where(Tariff.id == tariff_id))


async def test_2_8_tariff_with_subscriptions_cannot_be_deleted(db_session: AsyncSession) -> None:
    """2.8: тариф, у которого есть или были подписки, удалить нельзя; без подписок — можно."""
    used, unused = _tariff(name="Месяц"), _tariff(name="Год")
    await _add(db_session, used, unused)
    client = await _client(db_session)
    subscription = _subscription(client, used)
    subscription.deleted_at = NOW
    await _add(db_session, subscription)

    with pytest.raises(IntegrityError):
        await _delete_tariff_in_savepoint(db_session, used.id)
    await db_session.execute(delete(Tariff).where(Tariff.id == unused.id))

    assert list(await db_session.scalars(select(Tariff.name))) == ["Месяц"]


# --- Подписки ---


async def test_panel_user_belongs_to_one_subscription(db_session: AsyncSession) -> None:
    """Подписка — ровно один пользователь панели; один пользователь панели — одна подписка."""
    tariff = _tariff()
    await _add(db_session, tariff)
    client = await _client(db_session)
    await _add(db_session, _subscription(client, tariff, panel_user_id=7))

    await _assert_rejected(db_session, _subscription(client, tariff, panel_user_id=7))


async def test_subscription_without_tariff_is_allowed(db_session: AsyncSession) -> None:
    """Усыновлённая подписка может быть «без тарифа», пока к ней не применён тариф."""
    client = await _client(db_session)

    await _add(db_session, _subscription(client, None))


async def test_3_25_paid_segment_carries_its_cost(db_session: AsyncSession) -> None:
    """3.25: применённая покупка добавляет отрезок срока со стоимостью, равной оплате."""
    tariff = _tariff()
    await _add(db_session, tariff)
    client = await _client(db_session)
    subscription = _subscription(client, tariff)
    payment = _payment(client, tariff, state=PaymentState.APPLIED)
    await _add(db_session, subscription, payment)

    await _add(
        db_session,
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.PAYMENT,
            payment_id=payment.id,
            starts_at=NOW,
            ends_at=NOW + timedelta(days=30),
            cost=Decimal("199.00"),
        ),
    )

    cost = await db_session.scalar(select(func.sum(TermSegment.cost)))
    assert cost == Decimal("199.00")


@pytest.mark.parametrize("kind", [SegmentKind.TRIAL, SegmentKind.GRANTED, SegmentKind.MIGRATED])
async def test_3_26_gifted_segment_costs_nothing(
    db_session: AsyncSession, kind: SegmentKind
) -> None:
    """3.26, 0012: триал, выданные и перенесённые дни — отрезки нулевой стоимости."""
    client = await _client(db_session)
    subscription = _subscription(client, None)
    await _add(db_session, subscription)
    segment = {"subscription_id": subscription.id, "kind": kind, "starts_at": NOW}

    await _add(db_session, TermSegment(**segment, ends_at=NOW + timedelta(days=3)))
    await _assert_rejected(
        db_session, TermSegment(**segment, ends_at=NOW + timedelta(days=3), cost=Decimal("1"))
    )


async def test_segments_are_consistent(db_session: AsyncSession) -> None:
    """Оплаченный отрезок ссылается на платёж; пустой отрезок и пустой пакет не допускаются."""
    client = await _client(db_session)
    subscription = _subscription(client, None)
    await _add(db_session, subscription)
    base = {"subscription_id": subscription.id, "starts_at": NOW}

    await _assert_rejected(
        db_session, TermSegment(**base, kind=SegmentKind.PAYMENT, ends_at=NOW + timedelta(days=1))
    )
    await _assert_rejected(db_session, TermSegment(**base, kind=SegmentKind.GRANTED, ends_at=NOW))
    await _assert_rejected(
        db_session,
        TrafficSegment(subscription_id=subscription.id, kind=SegmentKind.GRANTED, bytes=0),
    )


# --- Платежи ---


async def test_repeated_provider_confirmation_maps_to_one_payment(db_session: AsyncSession) -> None:
    """Сквозное правило 3: один счёт провайдера — один платёж в магазине."""
    tariff = _tariff()
    await _add(db_session, tariff)
    client = await _client(db_session)
    await _add(db_session, _payment(client, tariff, provider_payment_id="pay-1"))

    await _assert_rejected(db_session, _payment(client, tariff, provider_payment_id="pay-1"))


async def test_payment_conditions_and_client_are_required_unless_unknown(
    db_session: AsyncSession,
) -> None:
    """Сквозное правило 2 и 4.23: у обычного платежа есть клиент и зафиксированные условия;
    у неизвестного их нет, пока команда его не привяжет."""
    tariff = _tariff()
    await _add(db_session, tariff)
    client = await _client(db_session)

    await _assert_rejected(db_session, _payment(None, tariff))
    await _assert_rejected(db_session, _payment(client, tariff, tariff_snapshot=None))
    await _add(
        db_session,
        _payment(
            None,
            None,
            purpose=None,
            is_unknown=True,
            state=PaymentState.PAID_NOT_APPLIED,
            tariff_snapshot=None,
        ),
    )


async def test_zero_amount_payment_has_no_provider_invoice(db_session: AsyncSession) -> None:
    """0033: платёж с нулевой суммой (скидка 100%) не создаёт счёта у провайдера."""
    tariff = _tariff()
    await _add(db_session, tariff)
    client = await _client(db_session)
    free = {"amount": Decimal("0"), "discount_amount": Decimal("199.00")}

    await _assert_rejected(db_session, _payment(client, tariff, **free))
    await _add(
        db_session,
        _payment(client, tariff, **free, provider=None, provider_payment_id=None),
    )


async def test_10_12_one_refund_per_payment(db_session: AsyncSession) -> None:
    """10.12: по платежу возможен один возврат — полный или на меньшую сумму."""
    tariff = _tariff()
    owner = TeamMember(telegram_id=100500, role=TeamRole.OWNER)
    await _add(db_session, tariff, owner)
    client = await _client(db_session)
    payment = _payment(client, tariff, state=PaymentState.PARTIALLY_REFUNDED)
    await _add(db_session, payment)
    refund = {
        "payment_id": payment.id,
        "method": RefundMethod.MONEY,
        "performed_manually": True,
        "performed_by_id": owner.id,
    }

    await _add(db_session, Refund(**refund, amount=Decimal("100.00")))
    await _assert_rejected(db_session, Refund(**refund, amount=Decimal("99.00")))


async def test_10_6_refund_request_is_closed_by_refund_or_rejection(
    db_session: AsyncSession,
) -> None:
    """10.6: запрос на возврат открыт, пока не закрыт возвратом или отказом."""
    owner = TeamMember(telegram_id=100500, role=TeamRole.OWNER)
    await _add(db_session, owner)
    client = await _client(db_session)
    request = {"client_id": client.id, "comment": "просит вернуть", "created_by_id": owner.id}

    await _add(db_session, RefundRequest(**request))
    await _add(
        db_session,
        RefundRequest(
            **request,
            state=RefundRequestState.REJECTED,
            closed_at=NOW,
            closed_by_id=owner.id,
            close_comment="подписка использована",
        ),
    )
    await _assert_rejected(db_session, RefundRequest(**request, state=RefundRequestState.DONE))


# --- Бонусный счёт ---


async def test_bonus_balance_is_the_sum_of_operations(db_session: AsyncSession) -> None:
    """0007: сумма на бонусном счёте — итог журнала операций, а не число в профиле."""
    client = await _client(db_session)
    await _add(
        db_session,
        BonusOperation(client_id=client.id, amount=Decimal("50.00"), reason=BonusReason.REFERRAL),
        BonusOperation(client_id=client.id, amount=Decimal("-30.00"), reason=BonusReason.PAYMENT),
    )

    balance = await db_session.scalar(
        select(func.sum(BonusOperation.amount)).where(BonusOperation.client_id == client.id)
    )
    assert balance == Decimal("20.00")
    await _assert_rejected(
        db_session,
        BonusOperation(client_id=client.id, amount=Decimal("0"), reason=BonusReason.COMPENSATION),
    )
