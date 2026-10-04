"""Журнал действий (4.25–4.26)

Revision ID: 1e3f2ea44ea3
Revises:
Create Date: 2026-10-05 00:31:34.860669

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1e3f2ea44ea3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 4.26: записи журнала нельзя изменить или удалить. Триггер срабатывает на любой
# запрос, откуда бы он ни пришёл. Если понадобится миграция данных журнала, она
# временно выключает триггер (ALTER TABLE ... DISABLE TRIGGER) — только миграцией.
FORBID_CHANGE_FUNCTION = """
CREATE FUNCTION journal_entries_forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Записи журнала нельзя изменить или удалить (4.26)';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            sa.Enum(
                "team_member",
                "client",
                "system",
                "panel",
                "provider",
                name="actor_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("actor_ref", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("subject_type", sa.String(length=50), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "success",
                "failure",
                name="outcome",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_type IN ('client', 'provider', 'team_member')) = (actor_ref IS NOT NULL)",
            name=op.f("ck_journal_entries_actor_ref_matches_type"),
        ),
        sa.CheckConstraint("action <> ''", name=op.f("ck_journal_entries_action_not_empty")),
        sa.CheckConstraint(
            "(subject_type IS NULL) = (subject_id IS NULL)",
            name=op.f("ck_journal_entries_subject_complete"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
    )
    op.create_index(
        "ix_journal_entries_subject",
        "journal_entries",
        ["subject_type", "subject_id", "id"],
        unique=False,
    )

    op.execute(FORBID_CHANGE_FUNCTION)
    op.execute(
        "CREATE TRIGGER journal_entries_forbid_update_delete"
        " BEFORE UPDATE OR DELETE ON journal_entries"
        " FOR EACH ROW EXECUTE FUNCTION journal_entries_forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER journal_entries_forbid_truncate"
        " BEFORE TRUNCATE ON journal_entries"
        " FOR EACH STATEMENT EXECUTE FUNCTION journal_entries_forbid_change()"
    )


def downgrade() -> None:
    # Удаление таблицы целиком триггеры не останавливают: это DDL, а не изменение записей
    op.drop_table("journal_entries")
    op.execute("DROP FUNCTION journal_entries_forbid_change()")
