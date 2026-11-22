"""Индекс журнала по времени записи для экрана «Журнал» (4.35, решение 0075)

Revision ID: 0bff0b45796c
Revises: 4b2fe08761b5
Create Date: 2026-10-23 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0bff0b45796c"
down_revision: str | Sequence[str] | None = "4b2fe08761b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новые сверху и фильтр по периоду; id — порядок записей с одинаковым временем
    op.create_index(
        "ix_journal_entries_occurred_at",
        "journal_entries",
        ["occurred_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_journal_entries_occurred_at", table_name="journal_entries")
