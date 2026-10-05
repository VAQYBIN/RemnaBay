"""Промокоды и их активации (блок 8)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Table,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import MONEY, CreatedAt, Money


class PromoKind(StrEnum):
    """Вид выгоды. В MVP — скидка в процентах и дни; остальное — v1."""

    PERCENT_DISCOUNT = "percent_discount"
    DAYS = "days"
    FIXED_DISCOUNT = "fixed_discount"
    BONUS = "bonus"


# Ограничение «только для выбранных тарифов» (8.2)
promo_code_tariffs = Table(
    "promo_code_tariffs",
    Base.metadata,
    Column(
        "promo_code_id",
        BigInteger,
        ForeignKey("promo_codes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tariff_id", BigInteger, ForeignKey("tariffs.id"), primary_key=True),
)


class PromoCode(Base):
    """Промокод. Одна активация на клиента действует всегда (8.2)."""

    __tablename__ = "promo_codes"
    __table_args__ = (
        # У каждого вида — ровно своё значение
        CheckConstraint(
            "(kind = 'percent_discount') = (discount_percent IS NOT NULL)",
            name="percent_matches_kind",
        ),
        CheckConstraint("(kind = 'days') = (days IS NOT NULL)", name="days_match_kind"),
        CheckConstraint(
            "(kind = 'fixed_discount') = (discount_amount IS NOT NULL)",
            name="amount_matches_kind",
        ),
        CheckConstraint("(kind = 'bonus') = (bonus_amount IS NOT NULL)", name="bonus_matches_kind"),
        # Скидка — от 1 до 100% (8.1)
        CheckConstraint("discount_percent BETWEEN 1 AND 100", name="percent_range"),
        CheckConstraint("days > 0", name="days_positive"),
        CheckConstraint("discount_amount > 0", name="discount_amount_positive"),
        CheckConstraint("bonus_amount > 0", name="bonus_amount_positive"),
        CheckConstraint("max_activations > 0", name="max_activations_positive"),
        CheckConstraint("code <> ''", name="code_not_empty"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[PromoKind] = mapped_column(str_enum_type(PromoKind, "promo_kind"))
    discount_percent: Mapped[int | None] = mapped_column(Integer)
    days: Mapped[int | None] = mapped_column(Integer)
    discount_amount: Mapped[Money | None] = mapped_column(MONEY)
    bonus_amount: Mapped[Money | None] = mapped_column(MONEY)
    # Ограничения (8.2); пусто — без ограничения
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_activations: Mapped[int | None] = mapped_column(Integer)
    only_without_payments: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # Отключённый промокод ведёт себя как несуществующий (8.3)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    created_at: Mapped[CreatedAt]


class ActivationState(StrEnum):
    # Дни ждут применения в панели (8.15)
    PENDING = "pending"
    APPLIED = "applied"
    # Отменённая активация не расходуется (4.32)
    CANCELLED = "cancelled"


class PromoActivation(Base):
    """Факт применения промокода: клиент, промокод, платёж или подписка, момент.

    Скидка засчитывается при применении платежа (8.6), поэтому один платёж —
    не больше одной активации (8.5). Уникальности «клиент + промокод» в базе нет:
    если клиент оплатил оба счёта одной покупки с одним промокодом, скидку
    получают оба (3.11), а превышение пишется в журнал.
    """

    __tablename__ = "promo_activations"
    __table_args__ = (
        CheckConstraint("payment_id IS NOT NULL OR subscription_id IS NOT NULL", name="has_target"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), unique=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    state: Mapped[ActivationState] = mapped_column(
        str_enum_type(ActivationState, "promo_activation_state")
    )
    created_at: Mapped[CreatedAt]
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
