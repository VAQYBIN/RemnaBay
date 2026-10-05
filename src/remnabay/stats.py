"""Цифры на главной админки (1.17–1.20).

Периоды и даты — в часовом поясе магазина (1.18): «сегодня» начинается в полночь
по времени магазина, «7 дней» и «30 дней» — включая сегодня.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, Refund, RefundMethod
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff

# Минимальная единица валюты учёта: для рубля — копейка (04-operator-settings, «Оплата»)
_MINOR_UNIT = Decimal("0.01")
# Подписки, истекающие в ближайшие 3 дня (1.17)
EXPIRING_WITHIN = timedelta(days=3)


class Period(StrEnum):
    TODAY = "today"
    WEEK = "7d"
    MONTH = "30d"


_DAYS = {Period.TODAY: 1, Period.WEEK: 7, Period.MONTH: 30}


def period_start(period: Period, now: datetime, time_zone: ZoneInfo) -> datetime:
    """Начало периода: полночь по времени магазина (1.18)."""
    today = now.astimezone(time_zone).date()
    first_day = today - timedelta(days=_DAYS[period] - 1)
    return datetime.combine(first_day, time.min, tzinfo=time_zone)


def _active(now: datetime) -> ColumnElement[bool]:
    """Активная подписка: не удалена и срок не истёк — статус в панели не важен (0051)."""
    return Subscription.deleted_at.is_(None) & (Subscription.expires_at > now)


@dataclass(frozen=True)
class HomeStats:
    # Все активные, кроме триалов, включая усыновлённые и без тарифа (1.17)
    active_paid: int
    active_trials: int
    # Впервые открыли бот за период, без усыновлённых
    new_clients: int
    # Сумма применённых платежей за период минус возвраты за период (1.19)
    revenue: Decimal
    payments: int
    expiring_soon: int
    period_start: datetime


async def _count(session: AsyncSession, query: ColumnElement[bool], model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(query)) or 0


async def home_stats(
    session: AsyncSession, period: Period, *, now: datetime, time_zone: ZoneInfo
) -> HomeStats:
    start = period_start(period, now, time_zone)
    applied = (
        Payment.applied_at.is_not(None)
        & (Payment.applied_at >= start)
        & (Payment.applied_at <= now)
    )
    # Бонусная часть (v1) — в bonus_amount, в выручку не входит: amount — деньги (1.19)
    paid = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(applied))
    refunded = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.method == RefundMethod.MONEY,
            Refund.performed_at >= start,
            Refund.performed_at <= now,
        )
    )
    return HomeStats(
        active_paid=await _count(session, _active(now) & ~Subscription.is_trial, Subscription),
        active_trials=await _count(session, _active(now) & Subscription.is_trial, Subscription),
        new_clients=await _count(
            session,
            Client.bot_started_at.is_not(None)
            & (Client.bot_started_at >= start)
            & (Client.bot_started_at <= now)
            & Client.adopted_at.is_(None),
            Client,
        ),
        revenue=(Decimal(paid or 0) - Decimal(refunded or 0)).quantize(_MINOR_UNIT),
        payments=await _count(session, applied, Payment),
        expiring_soon=await _count(session, _expiring(now), Subscription),
        period_start=start,
    )


def _expiring(now: datetime) -> ColumnElement[bool]:
    return _active(now) & (Subscription.expires_at <= now + EXPIRING_WITHIN)


@dataclass(frozen=True)
class ExpiringSubscription:
    subscription_id: int
    name: str
    is_trial: bool
    tariff_name: str | None
    expires_at: datetime
    client_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None


async def expiring_subscriptions(
    session: AsyncSession, *, now: datetime
) -> list[ExpiringSubscription]:
    """Список к цифре «истекают в ближайшие 3 дня» (1.17): раньше истекающие — выше."""
    rows = await session.execute(
        select(Subscription, Tariff.name, TelegramAccount)
        .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
        .outerjoin(TelegramAccount, TelegramAccount.client_id == Subscription.client_id)
        .where(_expiring(now))
        .order_by(Subscription.expires_at, Subscription.id)
    )
    result: list[ExpiringSubscription] = []
    for subscription, tariff_name, account in rows:
        if subscription.expires_at is None:
            continue
        result.append(
            ExpiringSubscription(
                subscription_id=subscription.id,
                name=subscription.name,
                is_trial=subscription.is_trial,
                tariff_name=tariff_name,
                expires_at=subscription.expires_at,
                client_id=subscription.client_id,
                telegram_id=account.telegram_id if account else None,
                username=account.username if account else None,
                first_name=account.first_name if account else None,
            )
        )
    return result
