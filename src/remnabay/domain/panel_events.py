"""Принятые события вебхуков панели — для однократного эффекта (4.2)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base
from remnabay.domain._types import CreatedAt
from remnabay.journal import JsonValue


class PanelEvent(Base):
    """Событие панели с верной подписью. Повтор того же события узнаётся по хэшу
    тела запроса: панель повторяет доставку тем же телом."""

    __tablename__ = "panel_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    body_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    received_at: Mapped[CreatedAt]
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
