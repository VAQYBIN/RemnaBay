"""Реферальные связи и коды (данные — MVP; награды — v1)."""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, false
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base
from remnabay.domain._types import CreatedAt


class ReferralLink(Base):
    """Связь «клиент A пригласил клиента B».

    У приглашённого не больше одного пригласившего, переназначить нельзя,
    пригласить самого себя нельзя (3.15).
    """

    __tablename__ = "referral_links"
    __table_args__ = (CheckConstraint("inviter_id <> invited_id", name="not_self"),)

    invited_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # Перенесена из старого бота (9.11)
    migrated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[CreatedAt]


class ReferralCode(Base):
    """Код в реферальной ссылке. Свой код клиента и коды старого бота ведут на
    того же клиента (9.13)."""

    __tablename__ = "referral_codes"
    __table_args__ = (CheckConstraint("code <> ''", name="code_not_empty"),)

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    migrated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[CreatedAt]
