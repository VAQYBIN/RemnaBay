"""Миграция: ход прогона, снимок панели, перенесённые клиенты (блок 9, решения 0070, 0071)

Revision ID: 4b2fe08761b5
Revises: e4a8c2d1f6b3
Create Date: 2026-10-16 22:26:58.822017

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4b2fe08761b5"
down_revision: str | Sequence[str] | None = "e4a8c2d1f6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("reading_panel", "planning", "applying", "done", "failed")


def upgrade() -> None:
    op.create_table(
        "migration_panel_users",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["migration_runs.id"],
            name=op.f("fk_migration_panel_users_run_id_migration_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "panel_user_id", name=op.f("pk_migration_panel_users")),
    )
    op.add_column("clients", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("old_bot_paid", sa.Boolean(), nullable=True))
    op.add_column(
        "subscriptions", sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("migration_runs", sa.Column("dry_run_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "migration_runs",
        sa.Column("state", sa.String(length=32), server_default="reading_panel", nullable=False),
    )
    # Прогоны до этой версии не шли: таблица была заготовкой (0015)
    op.execute("UPDATE migration_runs SET state = 'done'")
    op.create_check_constraint(
        op.f("ck_migration_runs_migration_run_state"),
        "migration_runs",
        "state IN ({})".format(", ".join(f"'{state}'" for state in STATES)),
    )
    op.add_column(
        "migration_runs",
        sa.Column(
            "file", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "migration_runs",
        sa.Column(
            "plan", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "migration_runs", sa.Column("processed", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("migration_runs", sa.Column("total", sa.Integer(), nullable=True))
    op.add_column("migration_runs", sa.Column("error", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_migration_runs_dry_run_id"), "migration_runs", ["dry_run_id"]
    )
    op.create_foreign_key(
        op.f("fk_migration_runs_dry_run_id_migration_runs"),
        "migration_runs",
        "migration_runs",
        ["dry_run_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_migration_runs_dry_run_id_migration_runs"), "migration_runs", type_="foreignkey"
    )
    op.drop_constraint(op.f("uq_migration_runs_dry_run_id"), "migration_runs", type_="unique")
    op.drop_column("migration_runs", "error")
    op.drop_column("migration_runs", "total")
    op.drop_column("migration_runs", "processed")
    op.drop_column("migration_runs", "plan")
    op.drop_column("migration_runs", "file")
    op.drop_constraint(
        op.f("ck_migration_runs_migration_run_state"), "migration_runs", type_="check"
    )
    op.drop_column("migration_runs", "state")
    op.drop_column("migration_runs", "dry_run_id")
    op.drop_column("subscriptions", "adopted_at")
    op.drop_column("clients", "old_bot_paid")
    op.drop_column("clients", "imported_at")
    op.drop_table("migration_panel_users")
