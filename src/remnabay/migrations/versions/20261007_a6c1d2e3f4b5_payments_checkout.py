"""Платежи: подтверждённая сумма (3.39), операция счёта (3.10, 3.11), новая подписка (4.22)

Revision ID: a6c1d2e3f4b5
Revises: e902cf5e1c75
Create Date: 2026-10-07 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a6c1d2e3f4b5"
down_revision: str | Sequence[str] | None = "e902cf5e1c75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("paid_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("payments", sa.Column("paid_currency", sa.String(length=3), nullable=True))
    op.add_column(
        "payments", sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column("new_subscription", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(op.f("ix_payments_operation_id"), "payments", ["operation_id"], unique=False)
    op.create_check_constraint(
        op.f("ck_payments_paid_amount_not_negative"), "payments", "paid_amount >= 0"
    )
    op.create_check_constraint(
        op.f("ck_payments_paid_complete"),
        "payments",
        "(paid_amount IS NULL) = (paid_currency IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_payments_paid_complete"), "payments", type_="check")
    op.drop_constraint(op.f("ck_payments_paid_amount_not_negative"), "payments", type_="check")
    op.drop_index(op.f("ix_payments_operation_id"), table_name="payments")
    op.drop_column("payments", "new_subscription")
    op.drop_column("payments", "operation_id")
    op.drop_column("payments", "paid_currency")
    op.drop_column("payments", "paid_amount")
