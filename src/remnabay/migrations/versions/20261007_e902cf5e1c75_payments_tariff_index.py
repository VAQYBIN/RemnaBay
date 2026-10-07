"""Индекс платежей по тарифу: проверка «по тарифу были платежи» (2.8)

Revision ID: e902cf5e1c75
Revises: 315f30fa8ca4
Create Date: 2026-10-07 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e902cf5e1c75"
down_revision: str | Sequence[str] | None = "315f30fa8ca4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_payments_tariff_id"), "payments", ["tariff_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_tariff_id"), table_name="payments")
