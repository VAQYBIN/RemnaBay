"""Подписка, которую закончил возврат: без напоминаний и «Подписка истекла» (10.13)

Revision ID: d5a0c7e3f912
Revises: c41d7e9a2b63
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a0c7e3f912"
down_revision: str | Sequence[str] | None = "c41d7e9a2b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("ended_by_refund_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "ended_by_refund_at")
