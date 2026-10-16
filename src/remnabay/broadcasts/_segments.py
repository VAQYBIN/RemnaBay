"""Сегменты — фильтры по данным клиентов (11.2, решение 0065).

Получатель — клиент с Telegram-аккаунтом: другого канала в MVP нет. Пометка «бот
заблокирован» из сегмента не исключает: бот можно разблокировать, не открывая его.
Состояние подписки — как его видит клиент в боте: «исчерпан трафик» — по панели,
«активна» — срок не истёк; отключённые в панели и удалённые подписки не считаются.
"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.broadcasts._content import Segment, SegmentKind
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.subscriptions import PanelUserStatus, Subscription


def _counted() -> ColumnElement[bool]:
    """Подписка, которая решает сегмент: не удалена и не отключена в панели."""
    return (
        (Subscription.client_id == Client.id)
        & Subscription.deleted_at.is_(None)
        & Subscription.panel_status.is_distinct_from(PanelUserStatus.DISABLED)
    )


def _running(now: datetime) -> ColumnElement[bool]:
    """Активна или исчерпала трафик, включая триал (0065)."""
    limited = Subscription.panel_status.is_not_distinct_from(PanelUserStatus.LIMITED)
    in_term = (
        Subscription.panel_status.is_distinct_from(PanelUserStatus.EXPIRED)
        & Subscription.expires_at.is_not(None)
        & (Subscription.expires_at > now)
    )
    return limited | in_term


def _has_running(now: datetime) -> ColumnElement[bool]:
    return exists().where(_counted(), _running(now))


def _all(_now: datetime) -> ColumnElement[bool]:
    return Client.id.is_not(None)


def _active(now: datetime) -> ColumnElement[bool]:
    return _has_running(now)


def _expired(now: datetime) -> ColumnElement[bool]:
    """Подписки есть, ни одной активной, и хотя бы одна истекла (0065)."""
    return ~_has_running(now) & exists().where(_counted(), ~_running(now))


_FILTERS: dict[SegmentKind, Callable[[datetime], ColumnElement[bool]]] = {
    SegmentKind.ALL: _all,
    SegmentKind.ACTIVE: _active,
    SegmentKind.EXPIRED: _expired,
}


def recipients_query(segment: Segment, now: datetime) -> Select[int]:
    """Клиенты сегмента, которым есть куда написать, — по возрастанию id."""
    return (
        select(Client.id)
        .where(exists().where(TelegramAccount.client_id == Client.id), _FILTERS[segment.kind](now))
        .order_by(Client.id)
    )


async def count_recipients(session: AsyncSession, segment: Segment, now: datetime) -> int:
    """Число получателей при выборе сегмента (11.2)."""
    query = recipients_query(segment, now).order_by(None).subquery()
    return await session.scalar(select(func.count()).select_from(query)) or 0
