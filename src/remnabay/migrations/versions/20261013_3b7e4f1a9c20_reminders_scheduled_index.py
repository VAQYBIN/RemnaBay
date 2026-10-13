"""Напоминания: индекс только по неотправленным (блок 7, 0014)

Revision ID: 3b7e4f1a9c20
Revises: 8cbec37ba841
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b7e4f1a9c20"
down_revision: str | Sequence[str] | None = "8cbec37ba841"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_reminders_due_at"), table_name="reminders")
    op.create_index(
        "ix_reminders_scheduled_due_at",
        "reminders",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("state = 'scheduled'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminders_scheduled_due_at",
        table_name="reminders",
        postgresql_where=sa.text("state = 'scheduled'"),
    )
    op.create_index(op.f("ix_reminders_due_at"), "reminders", ["due_at"], unique=False)
