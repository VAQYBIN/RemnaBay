"""Напоминания об окончании подписки (блок 7, решение 0014)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import CreatedAt


class ReminderState(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    # Наступили несколько — отправлено только самое позднее (7.7)
    SKIPPED = "skipped"
    # Дата окончания изменилась — напоминание заменено новым (7.9)
    CANCELLED = "cancelled"


class Reminder(Base):
    """Запланированное сообщение об окончании подписки.

    Момент отправки — дата окончания минус смещение (7.4). Одно напоминание на
    подписку, дату окончания и смещение: повторно не отправляется, пока дата
    окончания не изменилась (7.8).
    """

    __tablename__ = "reminders"
    __table_args__ = (UniqueConstraint("subscription_id", "expires_at", "offset_hours"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    # Часы до окончания; отрицательное — после окончания (7.1)
    offset_hours: Mapped[int] = mapped_column(Integer)
    # Дата окончания, от которой рассчитано напоминание
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[ReminderState] = mapped_column(
        str_enum_type(ReminderState, "reminder_state"),
        server_default=ReminderState.SCHEDULED.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
