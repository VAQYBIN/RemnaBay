"""Промокоды: ввод кода клиентом и защита от перебора (З8, 8.13)

Revision ID: a712058d1d18
Revises: 3b7e4f1a9c20
Create Date: 2026-10-15 00:36:35.554630

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a712058d1d18"
down_revision: str | Sequence[str] | None = "3b7e4f1a9c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK для колонок-перечислений создаёт сам sa.Enum(create_constraint=True)
    op.create_table(
        "promo_entries",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "awaiting",
            sa.Enum(
                "menu",
                "confirm",
                name="promo_input",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("tariff_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(awaiting = 'confirm') = (tariff_id IS NOT NULL)",
            name=op.f("ck_promo_entries_confirm_has_tariff"),
        ),
        sa.CheckConstraint(
            "(awaiting IS NULL) = (requested_at IS NULL)",
            name=op.f("ck_promo_entries_awaiting_has_moment"),
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0", name=op.f("ck_promo_entries_failed_attempts_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_promo_entries_client_id_clients")
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_promo_entries")),
    )


def downgrade() -> None:
    op.drop_table("promo_entries")
