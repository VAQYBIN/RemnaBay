"""Очередь фоновых задач (0024)

Revision ID: 7199301a65e9
Revises: 1e3f2ea44ea3
Create Date: 2026-10-05 01:14:14.471092

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7199301a65e9"
down_revision: str | Sequence[str] | None = "1e3f2ea44ea3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK для колонок-перечислений создаёт сам sa.Enum(create_constraint=True)
    op.create_table(
        "queue_keys",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_queue_keys")),
    )
    op.create_table(
        "queue_periodic_slots",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", "slot_start", name=op.f("pk_queue_periodic_slots")),
    )
    op.create_table(
        "queue_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "waiting_panel",
                "done",
                "failed",
                "cancelled",
                name="queue_task_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("retry_round", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_tasks")),
    )
    op.create_index(
        "ix_queue_tasks_runnable",
        "queue_tasks",
        ["run_at", "id"],
        unique=False,
        postgresql_where="status IN ('pending', 'waiting_panel')",
    )
    op.create_index(
        "ix_queue_tasks_unfinished_key",
        "queue_tasks",
        ["key", "id"],
        unique=False,
        postgresql_where="status IN ('pending', 'waiting_panel', 'failed')",
    )
    op.create_table(
        "queue_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("retry_round", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result",
            sa.Enum(
                "done",
                "error",
                "unavailable",
                "aborted",
                name="queue_attempt_result",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["queue_tasks.id"],
            name=op.f("fk_queue_attempts_task_id_queue_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_attempts")),
    )
    op.create_index("ix_queue_attempts_task", "queue_attempts", ["task_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_queue_attempts_task", table_name="queue_attempts")
    op.drop_table("queue_attempts")
    op.drop_index(
        "ix_queue_tasks_unfinished_key",
        table_name="queue_tasks",
        postgresql_where="status IN ('pending', 'waiting_panel', 'failed')",
    )
    op.drop_index(
        "ix_queue_tasks_runnable",
        table_name="queue_tasks",
        postgresql_where="status IN ('pending', 'waiting_panel')",
    )
    op.drop_table("queue_tasks")
    op.drop_table("queue_periodic_slots")
    op.drop_table("queue_keys")
