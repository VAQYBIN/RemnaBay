"""Платёж, возврат и запрос на возврат (0033)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import MONEY, CreatedAt, Money
from remnabay.journal import JsonValue


class PaymentPurpose(StrEnum):
    """За что платёж: покупка новой подписки, продление или смена тарифа."""

    PURCHASE = "purchase"
    RENEWAL = "renewal"
    TARIFF_CHANGE = "tariff_change"


class PaymentState(StrEnum):
    """Состояния платежа (01-domain, 0033)."""

    PENDING = "pending"
    PAID = "paid"
    APPLIED = "applied"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    PAID_NOT_APPLIED = "paid_not_applied"
    RESOLVED_MANUALLY = "resolved_manually"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class Payment(Base):
    """Оплата клиентом конкретной операции. Один платёж — один счёт у провайдера.

    Условия (цена, параметры тарифа, промокод) фиксируются в момент создания
    платежа (сквозное правило 2): `tariff_snapshot` хранит их как клиент видел.
    """

    __tablename__ = "payments"
    __table_args__ = (
        # Повторный вебхук об этой же оплате ничего не делает (сквозное правило 3)
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
        CheckConstraint(
            "(provider IS NULL) = (provider_payment_id IS NULL)", name="provider_complete"
        ),
        # Клиента нет только у неизвестного платежа, пока его не привяжет команда (4.23)
        CheckConstraint("is_unknown OR client_id IS NOT NULL", name="client_known"),
        CheckConstraint("is_unknown OR tariff_snapshot IS NOT NULL", name="conditions_fixed"),
        # Платёж с нулевой суммой не создаёт счёта у провайдера (скидка 100%)
        CheckConstraint("amount > 0 OR provider IS NULL", name="zero_amount_without_provider"),
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_not_negative"),
        CheckConstraint("bonus_amount >= 0", name="bonus_not_negative"),
        CheckConstraint("unused_value_amount >= 0", name="unused_value_not_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    # Пусто у покупки, пока подписка не создана
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    purpose: Mapped[PaymentPurpose | None] = mapped_column(
        str_enum_type(PaymentPurpose, "payment_purpose")
    )
    state: Mapped[PaymentState] = mapped_column(
        str_enum_type(PaymentState, "payment_state"), index=True
    )
    is_unknown: Mapped[bool] = mapped_column(Boolean, server_default=false())

    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"))
    # Параметры тарифа и цена на момент создания платежа (3.7, 3.37)
    # none_as_null: Python None — это SQL NULL, а не JSON-значение null
    tariff_snapshot: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))

    # Суммы — в валюте учёта: к оплате деньгами; скидка по промокоду; бонусами (v1);
    # неиспользованная стоимость прежнего тарифа при смене тарифа (v1)
    amount: Mapped[Money] = mapped_column(MONEY)
    discount_amount: Mapped[Money] = mapped_column(MONEY, server_default="0")
    bonus_amount: Mapped[Money] = mapped_column(MONEY, server_default="0")
    unused_value_amount: Mapped[Money] = mapped_column(MONEY, server_default="0")
    currency: Mapped[str] = mapped_column(String(3))

    # Счёт у провайдера; у платежа с нулевой суммой счёта нет
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_payment_id: Mapped[str | None] = mapped_column(String(128))
    payment_url: Mapped[str | None] = mapped_column(String(2048))
    # Повторное «Оплатить» на том же экране подтверждения не создаёт второй счёт (3.5)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)

    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefundMethod(StrEnum):
    MONEY = "money"
    # На бонусный счёт (v1)
    BONUS = "bonus"


class Refund(Base):
    """Возврат по платежу. В MVP — не больше одного на платёж (10.12)."""

    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), unique=True)
    amount: Mapped[Money] = mapped_column(MONEY)
    method: Mapped[RefundMethod] = mapped_column(str_enum_type(RefundMethod, "refund_method"))
    # Владелец вернул деньги сам, а не через провайдера (10.11)
    performed_manually: Mapped[bool] = mapped_column(Boolean)
    performed_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    performed_at: Mapped[CreatedAt]


class RefundRequestState(StrEnum):
    OPEN = "open"
    DONE = "done"
    REJECTED = "rejected"


class RefundRequest(Base):
    """Просьба клиента о возврате, зафиксированная командой (10.6)."""

    __tablename__ = "refund_requests"
    __table_args__ = (
        CheckConstraint("(state = 'open') = (closed_at IS NULL)", name="closed_matches_state"),
        CheckConstraint("(state = 'done') = (refund_id IS NOT NULL)", name="done_has_refund"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    state: Mapped[RefundRequestState] = mapped_column(
        str_enum_type(RefundRequestState, "refund_request_state"),
        server_default=RefundRequestState.OPEN.value,
    )
    comment: Mapped[str] = mapped_column(Text)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    created_at: Mapped[CreatedAt]
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    # Отказ — с комментарием (10.6)
    close_comment: Mapped[str | None] = mapped_column(Text)
    refund_id: Mapped[int | None] = mapped_column(ForeignKey("refunds.id"))
