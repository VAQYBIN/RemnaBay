"""Получатель рассылки, который ни разу не открывал бот (11.8, решение 0069)

Revision ID: e4a8c2d1f6b3
Revises: b7c3e1f2a9d4
Create Date: 2026-10-16 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4a8c2d1f6b3"
down_revision: str | Sequence[str] | None = "b7c3e1f2a9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_broadcast_recipients_broadcast_delivery_state"
BEFORE = ("pending", "sending", "delivered", "bot_blocked", "failed", "skipped")
AFTER = (*BEFORE, "not_started")


def _check(states: tuple[str, ...]) -> str:
    return "state IN ({})".format(", ".join(f"'{state}'" for state in states))


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "broadcast_recipients", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "broadcast_recipients", _check(AFTER))


def downgrade() -> None:
    # До этой версии отказ Telegram писать первым считался блокировкой
    op.execute("UPDATE broadcast_recipients SET state = 'bot_blocked' WHERE state = 'not_started'")
    op.drop_constraint(op.f(CONSTRAINT), "broadcast_recipients", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "broadcast_recipients", _check(BEFORE))
