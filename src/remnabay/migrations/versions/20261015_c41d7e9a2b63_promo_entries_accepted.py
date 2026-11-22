"""Промокоды: принятый код клиента — защита от подделанных данных кнопки (8.4, 8.10)

Revision ID: c41d7e9a2b63
Revises: a712058d1d18
Create Date: 2026-10-15 03:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41d7e9a2b63"
down_revision: str | Sequence[str] | None = "a712058d1d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("promo_entries", sa.Column("accepted_promo_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_promo_entries_accepted_promo_id_promo_codes"),
        "promo_entries",
        "promo_codes",
        ["accepted_promo_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_promo_entries_accepted_promo_id_promo_codes"), "promo_entries", type_="foreignkey"
    )
    op.drop_column("promo_entries", "accepted_promo_id")
