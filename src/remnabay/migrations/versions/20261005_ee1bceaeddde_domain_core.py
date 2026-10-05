"""Модель данных, часть 1: ядро коммерции (0015)

Revision ID: ee1bceaeddde
Revises: 7199301a65e9
Create Date: 2026-10-05 01:47:41.960433

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ee1bceaeddde"
down_revision: str | Sequence[str] | None = "7199301a65e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK для колонок-перечислений создаёт сам sa.Enum(create_constraint=True)
    op.create_table(
        "clients",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bot_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "broadcasts_disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clients")),
    )
    op.create_table(
        "tariffs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "term_unlimited",
                "term_quota",
                "term_package",
                "package_only",
                name="tariff_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "on_sale",
                "archived",
                "closed",
                name="tariff_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="on_sale",
            nullable=False,
        ),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("squad_uuids", postgresql.ARRAY(sa.Uuid()), server_default="{}", nullable=False),
        sa.Column("traffic_limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "traffic_reset_strategy",
            sa.Enum(
                "day",
                "week",
                "month",
                "month_from_creation",
                name="traffic_reset_strategy",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successor_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(type = 'package_only') = (duration_days IS NULL)",
            name=op.f("ck_tariffs_duration_matches_type"),
        ),
        sa.CheckConstraint(
            "(type = 'term_quota') = (traffic_reset_strategy IS NOT NULL)",
            name=op.f("ck_tariffs_reset_strategy_matches_type"),
        ),
        sa.CheckConstraint(
            "(type = 'term_unlimited') = (traffic_limit_bytes IS NULL)",
            name=op.f("ck_tariffs_traffic_limit_matches_type"),
        ),
        sa.CheckConstraint("name <> ''", name=op.f("ck_tariffs_name_not_empty")),
        sa.CheckConstraint("device_limit >= 0", name=op.f("ck_tariffs_device_limit_not_negative")),
        sa.CheckConstraint("duration_days > 0", name=op.f("ck_tariffs_duration_positive")),
        sa.CheckConstraint("price >= 0", name=op.f("ck_tariffs_price_not_negative")),
        sa.CheckConstraint("successor_id <> id", name=op.f("ck_tariffs_successor_not_self")),
        sa.CheckConstraint(
            "traffic_limit_bytes > 0", name=op.f("ck_tariffs_traffic_limit_positive")
        ),
        sa.ForeignKeyConstraint(
            ["successor_id"], ["tariffs.id"], name=op.f("fk_tariffs_successor_id_tariffs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tariffs")),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("tariff_id", sa.BigInteger(), nullable=True),
        sa.Column("is_trial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("panel_user_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_username", sa.String(length=64), nullable=False),
        sa.Column("panel_short_uuid", sa.String(length=64), nullable=False),
        sa.Column("subscription_url", sa.String(length=512), nullable=False),
        sa.Column(
            "panel_status",
            sa.Enum(
                "active",
                "disabled",
                "limited",
                "expired",
                name="panel_user_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_limit", sa.Integer(), nullable=True),
        sa.Column("traffic_limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column("traffic_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "traffic_reset_strategy",
            sa.Enum(
                "day",
                "week",
                "month",
                "month_from_creation",
                name="traffic_reset_strategy",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("panel_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("name <> ''", name=op.f("ck_subscriptions_name_not_empty")),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_subscriptions_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["tariff_id"], ["tariffs.id"], name=op.f("fk_subscriptions_tariff_id_tariffs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint("panel_user_id", name=op.f("uq_subscriptions_panel_user_id")),
        sa.UniqueConstraint("panel_username", name=op.f("uq_subscriptions_panel_username")),
    )
    op.create_index(
        op.f("ix_subscriptions_client_id"), "subscriptions", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_subscriptions_panel_short_uuid"),
        "subscriptions",
        ["panel_short_uuid"],
        unique=False,
    )
    op.create_index(
        op.f("ix_subscriptions_tariff_id"), "subscriptions", ["tariff_id"], unique=False
    )
    op.create_table(
        "team_members",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "assistant",
                name="team_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("client_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_team_members_client_id_clients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_members")),
    )
    op.create_index(
        "uq_team_members_active_telegram_id",
        "team_members",
        ["telegram_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "telegram_accounts",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=256), nullable=True),
        sa.Column("last_name", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_telegram_accounts_client_id_clients")
        ),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_telegram_accounts")),
    )
    op.create_index(
        op.f("ix_telegram_accounts_client_id"), "telegram_accounts", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_telegram_accounts_username"), "telegram_accounts", ["username"], unique=False
    )
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("team_member_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["team_member_id"],
            ["team_members.id"],
            name=op.f("fk_admin_sessions_team_member_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_sessions_token_hash")),
    )
    op.create_index(
        op.f("ix_admin_sessions_team_member_id"), "admin_sessions", ["team_member_id"], unique=False
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "assistant",
                name="team_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["accepted_by_id"],
            ["team_members.id"],
            name=op.f("fk_invitations_accepted_by_id_team_members"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["team_members.id"],
            name=op.f("fk_invitations_created_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "purchase",
                "renewal",
                "tariff_change",
                name="payment_purpose",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "paid",
                "applied",
                "declined",
                "expired",
                "cancelled",
                "paid_not_applied",
                "resolved_manually",
                "refunded",
                "partially_refunded",
                name="payment_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_unknown", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tariff_id", sa.BigInteger(), nullable=True),
        sa.Column("tariff_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "discount_amount", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False
        ),
        sa.Column(
            "bonus_amount", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False
        ),
        sa.Column(
            "unused_value_amount",
            sa.Numeric(precision=14, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column("payment_url", sa.String(length=2048), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(provider IS NULL) = (provider_payment_id IS NULL)",
            name=op.f("ck_payments_provider_complete"),
        ),
        sa.CheckConstraint(
            "amount > 0 OR provider IS NULL", name=op.f("ck_payments_zero_amount_without_provider")
        ),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_payments_amount_not_negative")),
        sa.CheckConstraint("bonus_amount >= 0", name=op.f("ck_payments_bonus_not_negative")),
        sa.CheckConstraint("discount_amount >= 0", name=op.f("ck_payments_discount_not_negative")),
        sa.CheckConstraint(
            "is_unknown OR client_id IS NOT NULL", name=op.f("ck_payments_client_known")
        ),
        sa.CheckConstraint(
            "is_unknown OR tariff_snapshot IS NOT NULL", name=op.f("ck_payments_conditions_fixed")
        ),
        sa.CheckConstraint(
            "unused_value_amount >= 0", name=op.f("ck_payments_unused_value_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_payments_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_payments_subscription_id_subscriptions"),
        ),
        sa.ForeignKeyConstraint(
            ["tariff_id"], ["tariffs.id"], name=op.f("fk_payments_tariff_id_tariffs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payments_idempotency_key")),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
    )
    op.create_index(op.f("ix_payments_client_id"), "payments", ["client_id"], unique=False)
    op.create_index(op.f("ix_payments_state"), "payments", ["state"], unique=False)
    op.create_index(
        op.f("ix_payments_subscription_id"), "payments", ["subscription_id"], unique=False
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "money",
                "bonus",
                name="refund_method",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("performed_manually", sa.Boolean(), nullable=False),
        sa.Column("performed_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "performed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_refunds_amount_positive")),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_refunds_payment_id_payments")
        ),
        sa.ForeignKeyConstraint(
            ["performed_by_id"],
            ["team_members.id"],
            name=op.f("fk_refunds_performed_by_id_team_members"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refunds")),
        sa.UniqueConstraint("payment_id", name=op.f("uq_refunds_payment_id")),
    )
    op.create_table(
        "term_segments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "payment",
                "granted",
                "trial",
                "promo_code",
                "migrated",
                "panel",
                name="segment_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'payment') = (payment_id IS NOT NULL)",
            name=op.f("ck_term_segments_payment_matches_kind"),
        ),
        sa.CheckConstraint(
            "kind = 'payment' OR cost = 0", name=op.f("ck_term_segments_gift_costs_nothing")
        ),
        sa.CheckConstraint("cost >= 0", name=op.f("ck_term_segments_cost_not_negative")),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_term_segments_not_empty")),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_term_segments_payment_id_payments")
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_term_segments_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_term_segments")),
    )
    op.create_index(
        op.f("ix_term_segments_subscription_id"), "term_segments", ["subscription_id"], unique=False
    )
    op.create_table(
        "traffic_segments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "payment",
                "granted",
                "trial",
                "promo_code",
                "migrated",
                "panel",
                name="segment_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=14, scale=2), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'payment') = (payment_id IS NOT NULL)",
            name=op.f("ck_traffic_segments_payment_matches_kind"),
        ),
        sa.CheckConstraint(
            "kind = 'payment' OR cost = 0", name=op.f("ck_traffic_segments_gift_costs_nothing")
        ),
        sa.CheckConstraint("bytes > 0", name=op.f("ck_traffic_segments_bytes_positive")),
        sa.CheckConstraint("cost >= 0", name=op.f("ck_traffic_segments_cost_not_negative")),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_traffic_segments_payment_id_payments")
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_traffic_segments_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_traffic_segments")),
    )
    op.create_index(
        op.f("ix_traffic_segments_subscription_id"),
        "traffic_segments",
        ["subscription_id"],
        unique=False,
    )
    op.create_table(
        "bonus_operations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "referral",
                "welcome",
                "refund",
                "compensation",
                "tariff_change",
                "promo_code",
                "payment",
                "payment_release",
                name="bonus_reason",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("refund_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_id", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_bonus_operations_amount_not_zero")),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_bonus_operations_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["team_members.id"],
            name=op.f("fk_bonus_operations_created_by_id_team_members"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_bonus_operations_payment_id_payments")
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["refunds.id"], name=op.f("fk_bonus_operations_refund_id_refunds")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bonus_operations")),
    )
    op.create_index(
        op.f("ix_bonus_operations_client_id"), "bonus_operations", ["client_id"], unique=False
    )
    op.create_table(
        "refund_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "open",
                "done",
                "rejected",
                name="refund_request_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_id", sa.BigInteger(), nullable=True),
        sa.Column("close_comment", sa.Text(), nullable=True),
        sa.Column("refund_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(state = 'done') = (refund_id IS NOT NULL)",
            name=op.f("ck_refund_requests_done_has_refund"),
        ),
        sa.CheckConstraint(
            "(state = 'open') = (closed_at IS NULL)",
            name=op.f("ck_refund_requests_closed_matches_state"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_refund_requests_client_id_clients")
        ),
        sa.ForeignKeyConstraint(
            ["closed_by_id"],
            ["team_members.id"],
            name=op.f("fk_refund_requests_closed_by_id_team_members"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["team_members.id"],
            name=op.f("fk_refund_requests_created_by_id_team_members"),
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["refunds.id"], name=op.f("fk_refund_requests_refund_id_refunds")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_requests")),
    )
    op.create_index(
        op.f("ix_refund_requests_client_id"), "refund_requests", ["client_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refund_requests_client_id"), table_name="refund_requests")
    op.drop_table("refund_requests")
    op.drop_index(op.f("ix_bonus_operations_client_id"), table_name="bonus_operations")
    op.drop_table("bonus_operations")
    op.drop_index(op.f("ix_traffic_segments_subscription_id"), table_name="traffic_segments")
    op.drop_table("traffic_segments")
    op.drop_index(op.f("ix_term_segments_subscription_id"), table_name="term_segments")
    op.drop_table("term_segments")
    op.drop_table("refunds")
    op.drop_index(op.f("ix_payments_subscription_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_state"), table_name="payments")
    op.drop_index(op.f("ix_payments_client_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_table("invitations")
    op.drop_index(op.f("ix_admin_sessions_team_member_id"), table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_index(op.f("ix_telegram_accounts_username"), table_name="telegram_accounts")
    op.drop_index(op.f("ix_telegram_accounts_client_id"), table_name="telegram_accounts")
    op.drop_table("telegram_accounts")
    op.drop_index(
        "uq_team_members_active_telegram_id",
        table_name="team_members",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("team_members")
    op.drop_index(op.f("ix_subscriptions_tariff_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_panel_short_uuid"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_client_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("tariffs")
    op.drop_table("clients")
