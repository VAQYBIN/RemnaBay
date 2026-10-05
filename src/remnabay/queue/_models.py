"""Таблицы очереди. Остальной код их не знает: только интерфейс `remnabay.queue`."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.journal import JsonValue


class TaskStatus(StrEnum):
    """Состояние операции очереди (`01-domain.md`, «Операция очереди»).

    «Выполняется» не хранится: выполняемая задача — это строка, заблокированная
    воркером, и попытка без времени окончания.
    """

    PENDING = "pending"
    WAITING_PANEL = "waiting_panel"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Незавершённые задачи держат очередь своего ключа (4.17, 4.27)
UNFINISHED = (TaskStatus.PENDING, TaskStatus.WAITING_PANEL, TaskStatus.FAILED)
# Задачи, которые воркер может взять, когда подойдёт их время
RUNNABLE = (TaskStatus.PENDING, TaskStatus.WAITING_PANEL)


class AttemptResult(StrEnum):
    DONE = "done"
    ERROR = "error"
    # Внешний сервис недоступен: попытка не считается в лимит (4.30)
    UNAVAILABLE = "unavailable"
    # Попытка оборвалась вместе с процессом воркера
    ABORTED = "aborted"


def _in_statuses(*statuses: TaskStatus) -> str:
    return "status IN ({})".format(", ".join(f"'{status}'" for status in statuses))


class QueueKey(Base):
    """Служебная отметка ключа порядка (0036).

    Постановка задачи с ключом блокирует эту строку до конца транзакции: задачи
    одного ключа ставятся по очереди, и порядок номеров совпадает с порядком
    коммитов (0024). Строку подписки при этом никто не ждёт.
    """

    __tablename__ = "queue_keys"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)


class QueueTask(Base):
    __tablename__ = "queue_tasks"
    __table_args__ = (
        Index(
            "ix_queue_tasks_runnable",
            "run_at",
            "id",
            postgresql_where=_in_statuses(*RUNNABLE),
        ),
        Index(
            "ix_queue_tasks_unfinished_key",
            "key",
            "id",
            postgresql_where=_in_statuses(*UNFINISHED),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    args: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    # Задачи с одним ключом выполняются строго по порядку; без ключа — в любом
    key: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[TaskStatus] = mapped_column(
        str_enum_type(TaskStatus, "queue_task_status"), server_default=TaskStatus.PENDING.value
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    # Ручной повтор открывает новый круг: лимит попыток и окно считаются заново
    retry_round: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueueAttempt(Base):
    """Попытка выполнения: история с текстом ошибок (0024, 4.21)."""

    __tablename__ = "queue_attempts"
    __table_args__ = (Index("ix_queue_attempts_task", "task_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("queue_tasks.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    retry_round: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[AttemptResult | None] = mapped_column(
        str_enum_type(AttemptResult, "queue_attempt_result")
    )
    error: Mapped[str | None] = mapped_column(Text)


class QueuePeriodicSlot(Base):
    """Слот периодической задачи: «имя + начало периода» вставляется один раз."""

    __tablename__ = "queue_periodic_slots"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
