"""Цифры на главной: подписки, новые клиенты, выручка, истекающие (1.17–1.20)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import PaymentState, Refund, RefundMethod
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.stats import Period, expiring_subscriptions, home_stats, period_start
from tests.domain_support import (
    add,
    make_client,
    make_payment,
    make_subscription,
    make_tariff,
    make_team_member,
)
from tests.web_support import Shop, running_shop

MOSCOW = ZoneInfo("Europe/Moscow")
# 6 октября 2026, 01:30 по Москве = 5 октября, 22:30 UTC: «сегодня» — по времени магазина
NOW = datetime(2026, 10, 5, 22, 30, tzinfo=UTC)
TODAY_START = datetime(2026, 10, 6, 0, 0, tzinfo=MOSCOW)


def test_1_18_periods_start_at_shop_midnight() -> None:
    """1.18: периоды считаются в часовом поясе магазина."""
    assert period_start(Period.TODAY, NOW, MOSCOW) == TODAY_START
    assert period_start(Period.WEEK, NOW, MOSCOW) == TODAY_START - timedelta(days=6)
    assert period_start(Period.MONTH, NOW, MOSCOW) == TODAY_START - timedelta(days=29)
    # В UTC это ещё 5 октября — другой «сегодня»
    assert period_start(Period.TODAY, NOW, ZoneInfo("UTC")).day == 5


async def _subscription(
    session: AsyncSession,
    *,
    expires_in: timedelta | None,
    is_trial: bool = False,
    deleted: bool = False,
    with_tariff: bool = True,
    telegram_id: int,
) -> Subscription:
    client = await make_client(session, telegram_id=telegram_id)
    tariff = make_tariff() if with_tariff else None
    if tariff:
        await add(session, tariff)
    subscription = make_subscription(client, tariff, panel_user_id=telegram_id)
    subscription.is_trial = is_trial
    subscription.expires_at = NOW + expires_in if expires_in is not None else None
    subscription.deleted_at = NOW if deleted else None
    await add(session, subscription)
    return subscription


async def test_1_17_active_paid_and_trials(db_session: AsyncSession) -> None:
    """1.17: активные платные — все активные, кроме триалов, включая без тарифа;
    триалы — отдельно. Истёкшие и удалённые не считаются."""
    await _subscription(db_session, expires_in=timedelta(days=10), telegram_id=1)
    await _subscription(db_session, expires_in=timedelta(days=10), with_tariff=False, telegram_id=2)
    await _subscription(db_session, expires_in=timedelta(days=1), is_trial=True, telegram_id=3)
    await _subscription(db_session, expires_in=-timedelta(days=1), telegram_id=4)
    await _subscription(db_session, expires_in=timedelta(days=10), deleted=True, telegram_id=5)

    stats = await home_stats(db_session, Period.TODAY, now=NOW, time_zone=MOSCOW)

    assert (stats.active_paid, stats.active_trials) == (2, 1)


async def test_1_17_adopted_subscription_counts_as_paid(db_session: AsyncSession) -> None:
    subscription = await _subscription(
        db_session, expires_in=timedelta(days=30), with_tariff=False, telegram_id=7
    )
    client = await db_session.get_one(Client, subscription.client_id)
    client.adopted_at = NOW - timedelta(days=100)
    await db_session.flush()

    stats = await home_stats(db_session, Period.TODAY, now=NOW, time_zone=MOSCOW)

    assert stats.active_paid == 1


async def test_1_17_new_clients_exclude_adopted(db_session: AsyncSession) -> None:
    """1.17: новые клиенты — впервые открыли бот за период, без усыновлённых."""
    clients = [
        Client(bot_started_at=TODAY_START + timedelta(minutes=10)),
        Client(bot_started_at=TODAY_START - timedelta(minutes=10)),
        Client(bot_started_at=TODAY_START - timedelta(days=5)),
        Client(bot_started_at=TODAY_START + timedelta(minutes=5), adopted_at=NOW),
        Client(),
    ]
    await add(db_session, *clients)

    today = await home_stats(db_session, Period.TODAY, now=NOW, time_zone=MOSCOW)
    week = await home_stats(db_session, Period.WEEK, now=NOW, time_zone=MOSCOW)

    assert (today.new_clients, week.new_clients) == (1, 3)


async def test_1_19_revenue_is_applied_minus_refunded(db_session: AsyncSession) -> None:
    """1.19: выручка — применённые платежи за период минус возвраты за период;
    бонусная часть не входит."""
    client = await make_client(db_session, telegram_id=1)
    owner = make_team_member()
    await add(db_session, owner)
    applied = make_payment(
        client,
        None,
        state=PaymentState.APPLIED,
        amount=Decimal("199.00"),
        bonus_amount=Decimal("50.00"),
        applied_at=TODAY_START + timedelta(minutes=1),
    )
    earlier = make_payment(
        client,
        None,
        state=PaymentState.REFUNDED,
        amount=Decimal("300.00"),
        applied_at=TODAY_START - timedelta(days=3),
    )
    pending = make_payment(client, None, state=PaymentState.PENDING, amount=Decimal("999.00"))
    await add(db_session, applied, earlier, pending)
    await add(
        db_session,
        Refund(
            payment_id=earlier.id,
            amount=Decimal("100.00"),
            method=RefundMethod.MONEY,
            performed_manually=True,
            performed_by_id=owner.id,
            performed_at=TODAY_START + timedelta(minutes=2),
        ),
    )

    today = await home_stats(db_session, Period.TODAY, now=NOW, time_zone=MOSCOW)
    week = await home_stats(db_session, Period.WEEK, now=NOW, time_zone=MOSCOW)

    assert (today.revenue, today.payments) == (Decimal("99.00"), 1)
    assert (week.revenue, week.payments) == (Decimal("399.00"), 2)


async def test_1_17_expiring_within_three_days(db_session: AsyncSession) -> None:
    """1.17: истекающие в ближайшие 3 дня — число и список, раньше истекающие выше."""
    later = await _subscription(db_session, expires_in=timedelta(days=2), telegram_id=1)
    sooner = await _subscription(
        db_session, expires_in=timedelta(hours=5), is_trial=True, telegram_id=2
    )
    await _subscription(db_session, expires_in=timedelta(days=4), telegram_id=3)
    await _subscription(db_session, expires_in=-timedelta(hours=1), telegram_id=4)

    stats = await home_stats(db_session, Period.TODAY, now=NOW, time_zone=MOSCOW)
    listed = await expiring_subscriptions(db_session, now=NOW)

    assert stats.expiring_soon == 2
    assert [row.subscription_id for row in listed] == [sooner.id, later.id]
    assert listed[0].is_trial is True
    assert listed[0].telegram_id == 2


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


async def _signed_in(shop: Shop, role: TeamRole) -> TeamMember:
    member = make_team_member(telegram_id=600, role=role)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


async def test_1_17_owner_sees_revenue(shop: Shop) -> None:
    await _signed_in(shop, TeamRole.OWNER)

    response = await shop.http.get("/api/admin/stats", params={"period": "30d"})

    body = response.json()
    assert body["period"] == "30d"
    assert body["time_zone"] == "Europe/Moscow"
    assert body["revenue"] == {"amount": "0.00", "currency": "RUB", "payments": 0}


async def test_1_20_assistant_does_not_see_revenue(shop: Shop) -> None:
    """1.20: помощник цифр выручки не видит."""
    await _signed_in(shop, TeamRole.ASSISTANT)

    body = (await shop.http.get("/api/admin/stats")).json()

    assert body["revenue"] is None
    assert body["active_paid"] == 0


async def test_1_17_expiring_list_endpoint(shop: Shop) -> None:
    await _signed_in(shop, TeamRole.ASSISTANT)

    response = await shop.http.get("/api/admin/subscriptions/expiring")

    assert response.status_code == 200
    assert response.json() == []
