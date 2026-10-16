"""Неиспользованная часть платежа по модели отрезков (10.8, 10.9, решение 0065).

- Неиспользованная стоимость — пропорционально оставшемуся времени отрезка, с
  точностью до минуты, вниз до целой единицы валюты. Будущий отрезок не
  использован целиком, полностью использованный — ничего не стоит.
- Срок подписки сокращается на неиспользованную часть возвращаемого платежа: будущий
  отрезок убирается целиком, текущий обрезается по момент возврата, полностью
  использованный не меняется. Отрезки после него сдвигаются на убранное время.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.subscriptions import TermSegment

_MINUTE = timedelta(minutes=1)


def _minutes(delta: timedelta) -> int:
    return delta // _MINUTE


def unused_value(segments: Iterable[TermSegment], payment_id: int, at: datetime) -> Decimal:
    """Неиспользованная стоимость отрезков платежа на момент `at` (10.8)."""
    value = Decimal(0)
    for segment in segments:
        if segment.payment_id != payment_id or segment.ends_at <= at:
            continue
        if segment.starts_at >= at:
            value += segment.cost
            continue
        whole = _minutes(segment.ends_at - segment.starts_at)
        if whole > 0:
            value += segment.cost * _minutes(segment.ends_at - at) / whole
    return value.to_integral_value(rounding=ROUND_FLOOR)


def unused_time(segments: Iterable[TermSegment], payment_id: int, at: datetime) -> timedelta:
    """Неиспользованное время отрезков платежа на момент `at` — на него сокращается
    срок подписки (10.9)."""
    total = timedelta()
    for segment in segments:
        if segment.payment_id == payment_id and segment.ends_at > at:
            total += segment.ends_at - max(segment.starts_at, at)
    return total


async def cut_term(
    session: AsyncSession, segments: Sequence[TermSegment], payment_id: int, at: datetime
) -> timedelta:
    """Убирает из срока неиспользованную часть платежа (10.9); `segments` — все отрезки
    подписки по порядку. Возвращает убранное время."""
    removed = timedelta()
    for segment in segments:
        if removed:
            segment.starts_at -= removed
            segment.ends_at -= removed
        if segment.payment_id != payment_id or segment.ends_at <= at:
            continue
        if segment.starts_at >= at:
            removed += segment.ends_at - segment.starts_at
            await session.delete(segment)
        else:
            removed += segment.ends_at - at
            segment.ends_at = at
    await session.flush()
    return removed
