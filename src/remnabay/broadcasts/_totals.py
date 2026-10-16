"""Итог рассылки: сколько доставлено, у скольких бот заблокирован, сколько не открывали
бот, сколько ошибок (11.8, решение 0069)."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.broadcasts import BroadcastRecipient, DeliveryState
from remnabay.journal import JsonValue


@dataclass(frozen=True)
class Totals:
    recipients: int = 0
    delivered: int = 0
    bot_blocked: int = 0
    # Ни разу не открывали бот — отдельно от «бот заблокирован» (решение 0069)
    not_started: int = 0
    failed: int = 0
    # Не отправлено: рассылку остановили (11.7)
    skipped: int = 0
    # Ещё ждут отправки — прогресс идущей рассылки (11.5)
    pending: int = 0

    def as_details(self) -> dict[str, JsonValue]:
        return {
            "recipients": self.recipients,
            "delivered": self.delivered,
            "bot_blocked": self.bot_blocked,
            "not_started": self.not_started,
            "failed": self.failed,
            "skipped": self.skipped,
        }


async def totals_of(session: AsyncSession, broadcast_ids: Sequence[int]) -> dict[int, Totals]:
    rows = await session.execute(
        select(BroadcastRecipient.broadcast_id, BroadcastRecipient.state, func.count())
        .where(BroadcastRecipient.broadcast_id.in_(list(broadcast_ids)))
        .group_by(BroadcastRecipient.broadcast_id, BroadcastRecipient.state)
    )
    counts: dict[int, dict[DeliveryState, int]] = {id_: {} for id_ in broadcast_ids}
    for broadcast_id, state, count in rows:
        counts[broadcast_id][state] = count
    return {
        broadcast_id: Totals(
            recipients=sum(by_state.values()),
            delivered=by_state.get(DeliveryState.DELIVERED, 0),
            bot_blocked=by_state.get(DeliveryState.BOT_BLOCKED, 0),
            not_started=by_state.get(DeliveryState.NOT_STARTED, 0),
            failed=by_state.get(DeliveryState.FAILED, 0),
            skipped=by_state.get(DeliveryState.SKIPPED, 0),
            pending=by_state.get(DeliveryState.PENDING, 0) + by_state.get(DeliveryState.SENDING, 0),
        )
        for broadcast_id, by_state in counts.items()
    }
