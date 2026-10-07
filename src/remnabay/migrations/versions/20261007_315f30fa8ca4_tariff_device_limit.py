"""Лимит устройств тарифа не меньше 1 (0054)

Revision ID: 315f30fa8ca4
Revises: ceb549393a05
Create Date: 2026-10-07 10:07:20.308669

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "315f30fa8ca4"
down_revision: str | Sequence[str] | None = "ceb549393a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tariffs_device_limit_not_negative"), "tariffs", type_="check")
    op.create_check_constraint(
        op.f("ck_tariffs_device_limit_positive"), "tariffs", "device_limit >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tariffs_device_limit_positive"), "tariffs", type_="check")
    op.create_check_constraint(
        op.f("ck_tariffs_device_limit_not_negative"), "tariffs", "device_limit >= 0"
    )
