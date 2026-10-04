"""Рассылка (блок 11)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import CreatedAt
from remnabay.journal import JsonValue


class BroadcastState(StrEnum):
    DRAFT = "draft"
    SENDING = "sending"
    # Остановлена владельцем: оставшимся получателям сообщение не отправляется (11.7)
    STOPPED = "stopped"
    FINISHED = "finished"


class Broadcast(Base):
    """Сообщение владельца сегменту клиентов."""

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Текст с форматированием, изображение и кнопки (11.1) — структуру задаёт блок 11
    content: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    # Сегмент — фильтр по данным клиентов; новые сегменты не меняют механику рассылки
    segment: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    state: Mapped[BroadcastState] = mapped_column(
        str_enum_type(BroadcastState, "broadcast_state"),
        server_default=BroadcastState.DRAFT.value,
    )
    created_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    created_at: Mapped[CreatedAt]
    # Состав получателей фиксируется в момент подтверждения (11.4)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryState(StrEnum):
    PENDING = "pending"
    # Отправка началась: если она оборвалась сбоем, сообщение повторно не уходит (11.6)
    SENDING = "sending"
    DELIVERED = "delivered"
    BOT_BLOCKED = "bot_blocked"
    FAILED = "failed"
    # Рассылку остановили до отправки этому получателю (11.7)
    SKIPPED = "skipped"


class BroadcastRecipient(Base):
    """Получатель рассылки и состояние доставки. Каждый — не больше одного раза (11.6)."""

    __tablename__ = "broadcast_recipients"

    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    state: Mapped[DeliveryState] = mapped_column(
        str_enum_type(DeliveryState, "broadcast_delivery_state"),
        server_default=DeliveryState.PENDING.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
