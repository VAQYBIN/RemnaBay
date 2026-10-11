"""Инструкция и переходник: конфиги Subscription Page, токены, конфиг подписки (12.1, 12.7, 12.14)

Revision ID: 8cbec37ba841
Revises: a6c1d2e3f4b5
Create Date: 2026-10-11 13:28:33.325744

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8cbec37ba841"
down_revision: str | Sequence[str] | None = "a6c1d2e3f4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subpage_configs",
        sa.Column("uuid", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uuid", name=op.f("pk_subpage_configs")),
    )
    op.create_table(
        "redirect_links",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("short_uuid", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("app_name", sa.String(length=256), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_redirect_links_subscription_id_subscriptions"),
        ),
        sa.PrimaryKeyConstraint("token", name=op.f("pk_redirect_links")),
        sa.UniqueConstraint(
            "subscription_id",
            "short_uuid",
            "platform",
            "app_name",
            "template",
            name=op.f("uq_redirect_links_subscription_id"),
        ),
    )
    op.create_index(
        op.f("ix_redirect_links_subscription_id"),
        "redirect_links",
        ["subscription_id"],
        unique=False,
    )
    op.add_column("subscriptions", sa.Column("subpage_config_uuid", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "subpage_config_uuid")
    op.drop_index(op.f("ix_redirect_links_subscription_id"), table_name="redirect_links")
    op.drop_table("redirect_links")
    op.drop_table("subpage_configs")
