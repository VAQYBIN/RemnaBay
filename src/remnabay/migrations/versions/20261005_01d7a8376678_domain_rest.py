"""Модель данных, часть 2: остальные сущности (0015)

Revision ID: 01d7a8376678
Revises: ee1bceaeddde
Create Date: 2026-10-05 01:55:35.474685

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "01d7a8376678"
down_revision: str | Sequence[str] | None = "ee1bceaeddde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK для колонок-перечислений создаёт сам sa.Enum(create_constraint=True)
    op.create_table(
        "panel_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("body_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panel_events")),
        sa.UniqueConstraint("body_hash", name=op.f("uq_panel_events_body_hash")),
    )
    op.create_table(
        "referral_codes",
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("migrated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("code <> ''", name=op.f("ck_referral_codes_code_not_empty")),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_referral_codes_client_id_clients")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_referral_codes")),
    )
    op.create_index(
        op.f("ix_referral_codes_client_id"), "referral_codes", ["client_id"], unique=False
    )
    op.create_table(
        "referral_links",
        sa.Column("invited_id", sa.BigInteger(), nullable=False),
        sa.Column("inviter_id", sa.BigInteger(), nullable=False),
        sa.Column("migrated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("inviter_id <> invited_id", name=op.f("ck_referral_links_not_self")),
        sa.ForeignKeyConstraint(
            ["invited_id"], ["clients.id"], name=op.f("fk_referral_links_invited_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["inviter_id"], ["clients.id"], name=op.f("fk_referral_links_inviter_id_clients")
        ),
        sa.PrimaryKeyConstraint("invited_id", name=op.f("pk_referral_links")),
    )
    op.create_index(
        op.f("ix_referral_links_inviter_id"), "referral_links", ["inviter_id"], unique=False
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("segment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "draft",
                "sending",
                "stopped",
                "finished",
                name="broadcast_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["team_members.id"],
            name=op.f("fk_broadcasts_created_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )
    op.create_table(
        "device_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("hwid", sa.String(length=256), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_device_records_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_device_records_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_records")),
        sa.UniqueConstraint(
            "subscription_id", "hwid", name=op.f("uq_device_records_subscription_id")
        ),
    )
    op.create_index(
        op.f("ix_device_records_client_id"), "device_records", ["client_id"], unique=False
    )
    op.create_index(op.f("ix_device_records_hwid"), "device_records", ["hwid"], unique=False)
    op.create_table(
        "migration_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "adoption",
                "import",
                name="migration_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column(
            "tariff_mapping",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "source", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "report", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("started_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["started_by_id"],
            ["team_members.id"],
            name=op.f("fk_migration_runs_started_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_runs")),
    )
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "percent_discount",
                "days",
                "fixed_discount",
                "bonus",
                name="promo_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("discount_percent", sa.Integer(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("discount_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("bonus_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_activations", sa.Integer(), nullable=True),
        sa.Column(
            "only_without_payments", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'bonus') = (bonus_amount IS NOT NULL)",
            name=op.f("ck_promo_codes_bonus_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind = 'days') = (days IS NOT NULL)", name=op.f("ck_promo_codes_days_match_kind")
        ),
        sa.CheckConstraint(
            "(kind = 'fixed_discount') = (discount_amount IS NOT NULL)",
            name=op.f("ck_promo_codes_amount_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind = 'percent_discount') = (discount_percent IS NOT NULL)",
            name=op.f("ck_promo_codes_percent_matches_kind"),
        ),
        sa.CheckConstraint("code <> ''", name=op.f("ck_promo_codes_code_not_empty")),
        sa.CheckConstraint("bonus_amount > 0", name=op.f("ck_promo_codes_bonus_amount_positive")),
        sa.CheckConstraint("days > 0", name=op.f("ck_promo_codes_days_positive")),
        sa.CheckConstraint(
            "discount_amount > 0", name=op.f("ck_promo_codes_discount_amount_positive")
        ),
        sa.CheckConstraint(
            "discount_percent BETWEEN 1 AND 100", name=op.f("ck_promo_codes_percent_range")
        ),
        sa.CheckConstraint(
            "max_activations > 0", name=op.f("ck_promo_codes_max_activations_positive")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["team_members.id"],
            name=op.f("fk_promo_codes_created_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_promo_codes_code")),
    )
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("offset_hours", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "scheduled",
                "sent",
                "skipped",
                "cancelled",
                name="reminder_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="scheduled",
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_reminders_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
        sa.UniqueConstraint(
            "subscription_id",
            "expires_at",
            "offset_hours",
            name=op.f("uq_reminders_subscription_id"),
        ),
    )
    op.create_index(op.f("ix_reminders_due_at"), "reminders", ["due_at"], unique=False)
    op.create_table(
        "trials",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column("imported", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "used_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_trials_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_trials_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_trials")),
    )
    op.create_table(
        "broadcast_recipients",
        sa.Column("broadcast_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "sending",
                "delivered",
                "bot_blocked",
                "failed",
                "skipped",
                name="broadcast_delivery_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            name=op.f("fk_broadcast_recipients_broadcast_id_broadcasts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_broadcast_recipients_client_id_clients")
        ),
        sa.PrimaryKeyConstraint("broadcast_id", "client_id", name=op.f("pk_broadcast_recipients")),
    )
    op.create_table(
        "promo_code_tariffs",
        sa.Column("promo_code_id", sa.BigInteger(), nullable=False),
        sa.Column("tariff_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["promo_code_id"],
            ["promo_codes.id"],
            name=op.f("fk_promo_code_tariffs_promo_code_id_promo_codes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tariff_id"], ["tariffs.id"], name=op.f("fk_promo_code_tariffs_tariff_id_tariffs")
        ),
        sa.PrimaryKeyConstraint("promo_code_id", "tariff_id", name=op.f("pk_promo_code_tariffs")),
    )
    op.create_table(
        "promo_activations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("promo_code_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "applied",
                "cancelled",
                name="promo_activation_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "payment_id IS NOT NULL OR subscription_id IS NOT NULL",
            name=op.f("ck_promo_activations_has_target"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_promo_activations_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_promo_activations_payment_id_payments")
        ),
        sa.ForeignKeyConstraint(
            ["promo_code_id"],
            ["promo_codes.id"],
            name=op.f("fk_promo_activations_promo_code_id_promo_codes"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_promo_activations_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_activations")),
        sa.UniqueConstraint("payment_id", name=op.f("uq_promo_activations_payment_id")),
    )
    op.create_index(
        op.f("ix_promo_activations_client_id"), "promo_activations", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_promo_activations_promo_code_id"),
        "promo_activations",
        ["promo_code_id"],
        unique=False,
    )
    op.add_column("payments", sa.Column("promo_code_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_payments_promo_code_id_promo_codes"),
        "payments",
        "promo_codes",
        ["promo_code_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_payments_promo_code_id_promo_codes"), "payments", type_="foreignkey"
    )
    op.drop_column("payments", "promo_code_id")
    op.drop_index(op.f("ix_promo_activations_promo_code_id"), table_name="promo_activations")
    op.drop_index(op.f("ix_promo_activations_client_id"), table_name="promo_activations")
    op.drop_table("promo_activations")
    op.drop_table("promo_code_tariffs")
    op.drop_table("broadcast_recipients")
    op.drop_table("trials")
    op.drop_index(op.f("ix_reminders_due_at"), table_name="reminders")
    op.drop_table("reminders")
    op.drop_table("promo_codes")
    op.drop_table("migration_runs")
    op.drop_index(op.f("ix_device_records_hwid"), table_name="device_records")
    op.drop_index(op.f("ix_device_records_client_id"), table_name="device_records")
    op.drop_table("device_records")
    op.drop_table("broadcasts")
    op.drop_index(op.f("ix_referral_links_inviter_id"), table_name="referral_links")
    op.drop_table("referral_links")
    op.drop_index(op.f("ix_referral_codes_client_id"), table_name="referral_codes")
    op.drop_table("referral_codes")
    op.drop_table("panel_events")
