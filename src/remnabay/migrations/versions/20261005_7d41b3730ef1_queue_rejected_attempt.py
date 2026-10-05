"""Итог попытки «отказано»: действие точно не выполнено, повтор безопасен (4.33)

Revision ID: 7d41b3730ef1
Revises: 4a2802c37716
Create Date: 2026-10-05 23:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d41b3730ef1"
down_revision: str | Sequence[str] | None = "4a2802c37716"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_queue_attempts_queue_attempt_result"
BEFORE = ("done", "error", "unavailable", "aborted")
AFTER = (*BEFORE, "rejected")


def _check(results: tuple[str, ...]) -> str:
    return "result IN ({})".format(", ".join(f"'{result}'" for result in results))


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "queue_attempts", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "queue_attempts", _check(AFTER))


def downgrade() -> None:
    # До этой версии отказ был обычной ошибкой
    op.execute("UPDATE queue_attempts SET result = 'error' WHERE result = 'rejected'")
    op.drop_constraint(op.f(CONSTRAINT), "queue_attempts", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "queue_attempts", _check(BEFORE))
