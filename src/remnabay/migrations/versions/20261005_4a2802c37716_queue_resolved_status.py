"""Состояние «решена вручную» у задачи очереди (4.31)

Revision ID: 4a2802c37716
Revises: 6291572b61a1
Create Date: 2026-10-05 22:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a2802c37716"
down_revision: str | Sequence[str] | None = "6291572b61a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_queue_tasks_queue_task_status"
BEFORE = ("pending", "waiting_panel", "done", "failed", "cancelled")
AFTER = (*BEFORE, "resolved")


def _check(statuses: tuple[str, ...]) -> str:
    return "status IN ({})".format(", ".join(f"'{status}'" for status in statuses))


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "queue_tasks", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "queue_tasks", _check(AFTER))


def downgrade() -> None:
    # Решённые вручную задачи завершены — до этой версии так выглядели отменённые
    op.execute("UPDATE queue_tasks SET status = 'cancelled' WHERE status = 'resolved'")
    op.drop_constraint(op.f(CONSTRAINT), "queue_tasks", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "queue_tasks", _check(BEFORE))
