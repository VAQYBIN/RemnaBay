"""Бонусный счёт клиента — журнал операций (0007; данные — MVP, функция — v1)."""

from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import MONEY, CreatedAt, Money


class BonusReason(StrEnum):
    """Причина операции. Пополнить счёт деньгами и вывести с него нельзя."""

    REFERRAL = "referral"
    WELCOME = "welcome"
    REFUND = "refund"
    COMPENSATION = "compensation"
    TARIFF_CHANGE = "tariff_change"
    PROMO_CODE = "promo_code"
    # Списание при оплате и возврат резерва, если оплата не состоялась
    PAYMENT = "payment"
    PAYMENT_RELEASE = "payment_release"


class BonusOperation(Base):
    """Операция бонусного счёта. Сумма на счёте — итог журнала, а не число в профиле:
    начисление — положительная сумма, списание — отрицательная."""

    __tablename__ = "bonus_operations"
    __table_args__ = (CheckConstraint("amount <> 0", name="amount_not_zero"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    amount: Mapped[Money] = mapped_column(MONEY)
    reason: Mapped[BonusReason] = mapped_column(str_enum_type(BonusReason, "bonus_reason"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    refund_id: Mapped[int | None] = mapped_column(ForeignKey("refunds.id"))
    # Ручное начисление — только владелец и с комментарием
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]
