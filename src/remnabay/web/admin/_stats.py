"""Цифры на главной (1.17–1.20) и список истекающих подписок."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from remnabay.domain.team import TeamRole
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import get_setting, shop_time_zone
from remnabay.stats import Period, expiring_subscriptions, home_stats
from remnabay.web.admin._deps import DbSession, Member

router = APIRouter(tags=["stats"])


class RevenueOut(BaseModel):
    """Выручка и число оплат — только владельцу (1.20)."""

    amount: Decimal
    currency: str
    payments: int


class StatsOut(BaseModel):
    period: Period
    # Начало периода; даты в админке — в часовом поясе магазина (1.18)
    period_start: datetime
    time_zone: str
    active_paid: int
    active_trials: int
    new_clients: int
    expiring_soon: int
    revenue: RevenueOut | None


@router.get("/stats")
async def stats(session: DbSession, member: Member, period: Period = Period.TODAY) -> StatsOut:
    time_zone = await shop_time_zone(session)
    result = await home_stats(session, period, now=datetime.now(UTC), time_zone=time_zone)
    revenue = None
    if member.role == TeamRole.OWNER:
        revenue = RevenueOut(
            amount=result.revenue,
            currency=await get_setting(session, SHOP_CURRENCY),
            payments=result.payments,
        )
    return StatsOut(
        period=period,
        period_start=result.period_start,
        time_zone=time_zone.key,
        active_paid=result.active_paid,
        active_trials=result.active_trials,
        new_clients=result.new_clients,
        expiring_soon=result.expiring_soon,
        revenue=revenue,
    )


class ExpiringOut(BaseModel):
    subscription_id: int
    name: str
    is_trial: bool
    tariff_name: str | None
    expires_at: datetime
    client_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None


@router.get("/subscriptions/expiring")
async def expiring(session: DbSession, member: Member) -> list[ExpiringOut]:
    """Подписки, истекающие в ближайшие 3 дня (1.17)."""
    del member
    rows = await expiring_subscriptions(session, now=datetime.now(UTC))
    return [ExpiringOut.model_validate(row, from_attributes=True) for row in rows]
