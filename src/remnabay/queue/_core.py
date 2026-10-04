"""Задачи очереди: описание, постановка, разбор проваленных командой."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.queue._models import (
    UNFINISHED,
    QueueAttempt,
    QueueKey,
    QueueTask,
    TaskStatus,
)

# Вид объекта в журнале для записей об операциях очереди
JOURNAL_SUBJECT = "queue_task"


def task_subject(task_id: int) -> Subject:
    return Subject(JOURNAL_SUBJECT, task_id)


@dataclass(frozen=True)
class TaskContext:
    """Что получает задача при выполнении.

    `session` — транзакция воркера: изменения задачи и отметка о выполнении
    фиксируются вместе. Задача не вызывает `commit()` и `rollback()` сама.
    Новые задачи (4.28) ставятся через эту же сессию.
    """

    session: AsyncSession
    task_id: int
    attempt_number: int


type Handler[A: BaseModel] = Callable[[TaskContext, A], Awaitable[None]]


class RunnableTask(Protocol):
    """То, что воркер умеет выполнить: имя и разбор аргументов из JSON."""

    @property
    def name(self) -> str: ...

    async def run(self, context: TaskContext, raw_args: dict[str, JsonValue]) -> None: ...


class TaskDefinition[A: BaseModel]:
    """Вид задачи: имя, модель аргументов (Pydantic) и обработчик.

    Аргументы проверяются при постановке и ещё раз при выполнении: между ними
    может смениться версия кода.
    """

    def __init__(self, name: str, args_model: type[A], handler: Handler[A]) -> None:
        self._name = name
        self._args_model = args_model
        self._handler = handler

    @property
    def name(self) -> str:
        return self._name

    async def enqueue(
        self,
        session: AsyncSession,
        args: A,
        *,
        key: str | None = None,
        delay: timedelta | None = None,
    ) -> int:
        """Ставит задачу в транзакции сессии; откат транзакции отменяет и задачу.

        Задачи с одним `key` (например, `subscription:42`) выполняются строго в
        порядке постановки (4.17).
        """
        if key is not None:
            # Блокирует отметку ключа до конца транзакции: следующая постановка
            # с тем же ключом дождётся коммита этой (0024, 0036)
            await session.execute(
                insert(QueueKey)
                .values(key=key)
                .on_conflict_do_update(index_elements=[QueueKey.key], set_={"key": key})
            )
        run_at = func.clock_timestamp() + (delay or timedelta())
        result = await session.execute(
            insert(QueueTask)
            .values(name=self._name, args=args.model_dump(mode="json"), key=key, run_at=run_at)
            .returning(QueueTask.id)
        )
        return result.scalar_one()

    async def run(self, context: TaskContext, raw_args: dict[str, JsonValue]) -> None:
        await self._handler(context, self._args_model.model_validate(raw_args))


def task[A: BaseModel](name: str, args_model: type[A]) -> Callable[[Handler[A]], TaskDefinition[A]]:
    """Декоратор: превращает обработчик в вид задачи."""

    def decorate(handler: Handler[A]) -> TaskDefinition[A]:
        return TaskDefinition(name, args_model, handler)

    return decorate


@dataclass(frozen=True)
class RetryPolicy:
    """Повторы с нарастающими интервалами (4.14).

    Повторы прекращаются по первому из условий: исчерпано число попыток или
    следующая попытка выпала бы за окно, отсчитанное от первой попытки круга.
    Значения по умолчанию — из `04-operator-settings.md`: 1 час или 20 попыток.
    """

    first_delay: timedelta = timedelta(seconds=10)
    max_delay: timedelta = timedelta(minutes=5)
    max_attempts: int = 20
    max_age: timedelta = timedelta(hours=1)
    # Как часто проверять, не вернулась ли панель (4.30)
    unavailable_recheck: timedelta = timedelta(seconds=30)

    def delay_after(self, counted_attempts: int) -> timedelta:
        """Пауза перед следующей попыткой: удваивается с каждой неудачей."""
        # Степень ограничена: при большом лимите попыток timedelta переполнилась бы
        doublings = min(counted_attempts - 1, 32)
        return min(self.first_delay * 2**doublings, self.max_delay)


class TaskNotFailedError(Exception):
    """Повторить или отменить можно только окончательно проваленную задачу."""


async def _lock_failed(session: AsyncSession, task_id: int) -> QueueTask:
    task = await session.scalar(
        select(QueueTask)
        .where(QueueTask.id == task_id, QueueTask.status == TaskStatus.FAILED)
        .with_for_update(key_share=True)
    )
    if task is None:
        raise TaskNotFailedError(f"Задача {task_id} не в состоянии «провалена»")
    return task


async def retry_failed(session: AsyncSession, task_id: int, *, actor: Actor) -> None:
    """«Повторить» (4.31): задача снова в очереди с новым кругом попыток."""
    await _lock_failed(session, task_id)
    await session.execute(
        update(QueueTask)
        .where(QueueTask.id == task_id)
        .values(
            status=TaskStatus.PENDING,
            retry_round=QueueTask.retry_round + 1,
            run_at=func.clock_timestamp(),
        )
    )
    await record(
        session,
        actor=actor,
        action="queue.retried",
        subject=task_subject(task_id),
        outcome=Outcome.SUCCESS,
    )


async def cancel_failed(session: AsyncSession, task_id: int, *, actor: Actor, comment: str) -> None:
    """«Отменить» (4.31): задача не выполняется, следующие задачи ключа идут дальше (4.32).

    Последствия для клиента и данных (сообщение, неиспользованный триал) — забота
    того, кто вызывает отмену.
    """
    await _lock_failed(session, task_id)
    await session.execute(
        update(QueueTask)
        .where(QueueTask.id == task_id)
        .values(status=TaskStatus.CANCELLED, finished_at=func.clock_timestamp())
    )
    await record(
        session,
        actor=actor,
        action="queue.cancelled",
        subject=task_subject(task_id),
        outcome=Outcome.SUCCESS,
        details={"comment": comment},
    )


async def task_status(session: AsyncSession, task_id: int) -> TaskStatus | None:
    return await session.scalar(select(QueueTask.status).where(QueueTask.id == task_id))


async def waiting_behind(session: AsyncSession, task_id: int) -> int:
    """Сколько задач того же ключа ждут за этой (4.27)."""
    behind = QueueTask.__table__.alias("behind")
    count = await session.scalar(
        select(func.count())
        .select_from(behind)
        .join(QueueTask, QueueTask.key == behind.c.key)
        .where(
            QueueTask.id == task_id,
            behind.c.id > QueueTask.id,
            behind.c.status.in_(UNFINISHED),
        )
    )
    return count or 0


async def attempts_of(session: AsyncSession, task_id: int) -> list[QueueAttempt]:
    """История попыток задачи с текстом ошибок (4.21), от первой к последней."""
    result = await session.scalars(
        select(QueueAttempt).where(QueueAttempt.task_id == task_id).order_by(QueueAttempt.id)
    )
    return list(result)
