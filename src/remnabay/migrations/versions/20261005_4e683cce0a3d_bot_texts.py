"""Правки текстов бота оператором (0018)

Revision ID: 4e683cce0a3d
Revises: 01d7a8376678
Create Date: 2026-10-05 02:25:41.777054

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e683cce0a3d"
down_revision: str | Sequence[str] | None = "01d7a8376678"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_text_overrides",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("updated_by_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["team_members.id"],
            name=op.f("fk_bot_text_overrides_updated_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("key", "language", name=op.f("pk_bot_text_overrides")),
    )


def downgrade() -> None:
    op.drop_table("bot_text_overrides")
