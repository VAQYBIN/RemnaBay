"""Тариф — то, что продаётся (0009, 0010, 0011)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import MONEY, CreatedAt, Money


class TariffType(StrEnum):
    """Один из четырёх типов; свободная комбинация параметров не допускается (0009)."""

    TERM_UNLIMITED = "term_unlimited"
    TERM_QUOTA = "term_quota"
    TERM_PACKAGE = "term_package"
    PACKAGE_ONLY = "package_only"


class TariffState(StrEnum):
    ON_SALE = "on_sale"
    ARCHIVED = "archived"
    CLOSED = "closed"


class TrafficResetStrategy(StrEnum):
    """Стратегии сброса панели для квоты на период (0010)."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    # Раз в месяц от даты создания — по умолчанию
    MONTH_FROM_CREATION = "month_from_creation"


_SQUAD_LIST: ARRAY[UUID] = ARRAY(Uuid(as_uuid=True))


class Tariff(Base):
    """Тариф. Тариф, у которого есть или были подписки, не удаляется (2.8):
    подписки и платежи ссылаются на него без каскада.
    """

    __tablename__ = "tariffs"
    __table_args__ = (
        # Срок есть у всех типов, кроме «только пакет» (у него дата окончания — 2099 год)
        CheckConstraint(
            "(type = 'package_only') = (duration_days IS NULL)", name="duration_matches_type"
        ),
        # Лимит трафика: у безлимита нет; у квоты — лимит периода; у пакета — размер пакета
        CheckConstraint(
            "(type = 'term_unlimited') = (traffic_limit_bytes IS NULL)",
            name="traffic_limit_matches_type",
        ),
        CheckConstraint(
            "(type = 'term_quota') = (traffic_reset_strategy IS NOT NULL)",
            name="reset_strategy_matches_type",
        ),
        CheckConstraint("duration_days > 0", name="duration_positive"),
        CheckConstraint("traffic_limit_bytes > 0", name="traffic_limit_positive"),
        CheckConstraint("price >= 0", name="price_not_negative"),
        CheckConstraint("device_limit >= 0", name="device_limit_not_negative"),
        CheckConstraint("successor_id <> id", name="successor_not_self"),
        CheckConstraint("name <> ''", name="name_not_empty"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, server_default="")
    type: Mapped[TariffType] = mapped_column(str_enum_type(TariffType, "tariff_type"))
    state: Mapped[TariffState] = mapped_column(
        str_enum_type(TariffState, "tariff_state"), server_default=TariffState.ON_SALE.value
    )
    duration_days: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Money] = mapped_column(MONEY)
    device_limit: Mapped[int] = mapped_column(Integer)
    # Внутренние сквады панели (параметры описываются в терминах Remnawave)
    squad_uuids: Mapped[list[UUID]] = mapped_column(_SQUAD_LIST, server_default="{}")
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    traffic_reset_strategy: Mapped[TrafficResetStrategy | None] = mapped_column(
        str_enum_type(TrafficResetStrategy, "traffic_reset_strategy")
    )
    # Порядок показа клиенту (2.4)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    # Тариф-преемник при архивации или закрытии (v1)
    successor_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
