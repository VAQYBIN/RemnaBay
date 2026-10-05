"""Подписка и её срок: отрезки срока и пакеты трафика."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import MONEY, CreatedAt, Money
from remnabay.domain.tariffs import TrafficResetStrategy


class PanelUserStatus(StrEnum):
    """Состояние подписки определяет панель (активна, отключена, исчерпан трафик, истекла)."""

    ACTIVE = "active"
    DISABLED = "disabled"
    LIMITED = "limited"
    EXPIRED = "expired"


class Subscription(Base):
    """Коммерческая обёртка над пользователем панели: «этот клиент купил такой-то
    тариф, и вот его пользователь в панели».

    Пользователь панели для подписки никогда не пересоздаётся: строка подписки
    появляется, когда пользователь уже создан в панели.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("name <> ''", name="name_not_empty"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # Название задаёт клиент; первая — «Основная», следующие — «Подписка 2», «Подписка 3»
    name: Mapped[str] = mapped_column(String(64))
    # Пусто — «без тарифа»: только у усыновлённой подписки, пока к ней не применён тариф
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), index=True)
    # Пока подписка триальная, вместо «Продлить» — «Купить подписку» (3.36);
    # покупка применяется к ней же (3.16) и снимает признак
    is_trial: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[CreatedAt]
    # Пользователя удалили в панели: клиент подписку не видит, история сохраняется (4.6)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Пользователь панели. В панели 3.4.4 он адресуется числовым id; ссылка — по shortUuid
    panel_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    panel_username: Mapped[str] = mapped_column(String(64), unique=True)
    panel_short_uuid: Mapped[str] = mapped_column(String(64), index=True)
    # Ссылку магазин сам не меняет; отозванную в панели — принимает новую (4.29)
    subscription_url: Mapped[str] = mapped_column(String(512))

    # Последние известные данные о доступе из панели: показываются клиенту, пока
    # панель недоступна (4.12). Источник истины — панель (0005)
    panel_status: Mapped[PanelUserStatus | None] = mapped_column(
        str_enum_type(PanelUserStatus, "panel_user_status")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_limit: Mapped[int | None] = mapped_column(Integer)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    traffic_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    traffic_reset_strategy: Mapped[TrafficResetStrategy | None] = mapped_column(
        str_enum_type(TrafficResetStrategy, "traffic_reset_strategy")
    )
    panel_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def operations_key(subscription_id: int) -> str:
    """Ключ порядка операций подписки в очереди: они выполняются строго по очереди (4.17)."""
    return f"subscription:{subscription_id}"


class SegmentKind(StrEnum):
    """Откуда отрезок. Стоимость есть только у оплаченного; остальные — подаренные (0012)."""

    PAYMENT = "payment"
    # Выдача дней командой, компенсации (10.3)
    GRANTED = "granted"
    TRIAL = "trial"
    PROMO_CODE = "promo_code"
    MIGRATED = "migrated"
    # Добавлено напрямую в панели (4.3)
    PANEL = "panel"


def _segment_checks() -> tuple[CheckConstraint, ...]:
    # Ограничение привязывается к одной таблице, поэтому у каждой таблицы — свои объекты
    return (
        CheckConstraint(
            "(kind = 'payment') = (payment_id IS NOT NULL)", name="payment_matches_kind"
        ),
        CheckConstraint("cost >= 0", name="cost_not_negative"),
        # Подаренные отрезки стоят ноль: возвращается только фактически заплаченное (0012)
        CheckConstraint("kind = 'payment' OR cost = 0", name="gift_costs_nothing"),
    )


class TermSegment(Base):
    """Отрезок срока подписки со своей стоимостью (3.25, 3.26).

    Срок подписки — последовательность отрезков; при сокращении даты в панели
    отрезки срезаются с конца (4.4). Меняются только после применения в панели (4.34).
    """

    __tablename__ = "term_segments"
    __table_args__ = (
        *_segment_checks(),
        CheckConstraint("ends_at > starts_at", name="not_empty"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    kind: Mapped[SegmentKind] = mapped_column(str_enum_type(SegmentKind, "segment_kind"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cost: Mapped[Money] = mapped_column(MONEY, server_default="0")
    created_at: Mapped[CreatedAt]


class TrafficSegment(Base):
    """Пакет трафика со своей стоимостью; расходуются от старых к новым (данные для v1)."""

    __tablename__ = "traffic_segments"
    __table_args__ = (
        *_segment_checks(),
        CheckConstraint("bytes > 0", name="bytes_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    kind: Mapped[SegmentKind] = mapped_column(str_enum_type(SegmentKind, "segment_kind"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    bytes: Mapped[int] = mapped_column(BigInteger)
    cost: Mapped[Money] = mapped_column(MONEY, server_default="0")
    created_at: Mapped[CreatedAt]
