"""«Требуют внимания» на главной админки (4.20, 4.27, 4.30, 4.31).

Счётчик — проваленные операции и платежи «оплачен — не применён» (включая
неизвестные). Операции «ждут панель» в счётчик не входят: для них отдельная
строка «ждут панель: N» (4.30). Экран — этап 4.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment
from remnabay.payments import apply_payment, payments_needing_attention
from remnabay.queue import FailedTask, TaskRegistry, failed_tasks, waiting_panel_count


@dataclass(frozen=True)
class Attention:
    # Проваленные операции, кроме применения платежей: платёж показывается сам (4.31)
    operations: list[FailedTask]
    payments: list[Payment]
    waiting_panel: int

    @property
    def count(self) -> int:
        """Счётчик «Требуют внимания» на главной (4.20)."""
        return len(self.operations) + len(self.payments)


async def attention(session: AsyncSession, tasks: TaskRegistry) -> Attention:
    operations = [
        failed for failed in await failed_tasks(session, tasks) if failed.name != apply_payment.name
    ]
    return Attention(
        operations=operations,
        payments=await payments_needing_attention(session),
        waiting_panel=await waiting_panel_count(session),
    )
