"""Рассылка: изображение и одна идущая рассылка (11.1, 11.13)

Revision ID: b7c3e1f2a9d4
Revises: d5a0c7e3f912
Create Date: 2026-10-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c3e1f2a9d4"
down_revision: str | Sequence[str] | None = "d5a0c7e3f912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broadcast_images",
        sa.Column("broadcast_id", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            name=op.f("fk_broadcast_images_broadcast_id_broadcasts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("broadcast_id", name=op.f("pk_broadcast_images")),
    )
    op.create_index(
        "uq_broadcasts_one_sending",
        "broadcasts",
        ["state"],
        unique=True,
        postgresql_where="state = 'sending'",
    )


def downgrade() -> None:
    op.drop_index("uq_broadcasts_one_sending", table_name="broadcasts")
    op.drop_table("broadcast_images")
