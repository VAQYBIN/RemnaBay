"""Правки текстов бота, сделанные оператором."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base


class BotTextOverride(Base):
    """Текст, переписанный оператором, для ключа и языка. Сброс к тексту по
    умолчанию — удаление строки."""

    __tablename__ = "bot_text_overrides"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
