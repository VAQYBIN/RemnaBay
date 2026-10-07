"""Задачи очереди: описание, постановка, разбор проваленных командой."""

from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import func, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.queue._models import (
    UNFINISHED,
    AttemptResult,
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
    # Чем закончилась прошлая попытка. Задача, которую нельзя выполнить дважды
    # (сообщение клиенту, 4.33), повторяет действие, только если прошлая попытка
    # точно его не выполнила: `None` (попытка первая) или `REJECTED`
    previous_result: AttemptResult | None = None


class RejectedError(Exception):
    """Внешний сервис отказал так, что действие точно не выполнено: повтор безопасен.

    Попытка засчитывается в лимит, как обычная ошибка, но в истории отмечена
    отдельно — по ней следующая попытка знает, что действие можно повторить.
    """


class Ending(StrEnum):
    """Чем закончилась задача, которую не удалось выполнить автоматически."""

    # Попытки исчерпаны: задача ждёт команду (4.20, 4.31)
    FAILED = "failed"
    # Команда отменила: действие считается невыполненным (4.32)
    CANCELLED = "cancelled"
    # Команда отметила решённой вручную (4.31)
    RESOLVED = "resolved"


type Handler[A: BaseModel] = Callable[[TaskContext, A], Awaitable[None]]
# Обработчик исхода: последствия для данных и клиента — например, платёж становится
# «оплачен — не применён» (4.20), клиент получает «Не удалось выполнить действие» (4.32).
# Выполняется в транзакции, которая меняет состояние задачи
type EndingHook[A: BaseModel] = Callable[[AsyncSession, int, A], Awaitable[None]]


class RunnableTask(Protocol):
    """То, что очередь умеет выполнить: имя, разбор аргументов из JSON, исходы."""

    @property
    def name(self) -> str: ...

    @property
    def cancellable(self) -> bool: ...

    @property
    def needs_attention(self) -> bool: ...

    async def run(self, context: TaskContext, raw_args: dict[str, JsonValue]) -> None: ...

    def handles(self, ending: Ending) -> bool: ...

    async def ended(
        self, ending: Ending, session: AsyncSession, task_id: int, raw_args: dict[str, JsonValue]
    ) -> None: ...


class TaskDefinition[A: BaseModel]:
    """Вид задачи: имя, модель аргументов (Pydantic), обработчик и обработчики исходов.

    Аргументы проверяются при постановке и ещё раз при выполнении: между ними
    может смениться версия кода.
    """

    def __init__(
        self,
        name: str,
        args_model: type[A],
        handler: Handler[A],
        *,
        cancellable: bool = True,
        needs_attention: bool = True,
    ) -> None:
        self._name = name
        self._args_model = args_model
        self._handler = handler
        self._cancellable = cancellable
        self._needs_attention = needs_attention
        self._hooks: dict[Ending, EndingHook[A]] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def cancellable(self) -> bool:
        """Можно ли отменить проваленную задачу. Нельзя, если действие уже необратимо
        с другой стороны — например, смена даты при возврате: деньги уже вернули (4.31)."""
        return self._cancellable

    @property
    def needs_attention(self) -> bool:
        """Нужна ли команда, когда попытки исчерпаны. Нет — задача снимается: например,
        недоставленное сообщение — не ошибка (сквозное правило 4), а служебную сверку
        повторит следующий обход."""
        return self._needs_attention

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

    def _on(self, ending: Ending, hook: EndingHook[A]) -> EndingHook[A]:
        if ending in self._hooks:
            raise ValueError(f"Обработчик «{ending}» задачи {self._name} уже задан")
        self._hooks[ending] = hook
        return hook

    def on_failed(self, hook: EndingHook[A]) -> EndingHook[A]:
        """Декоратор: что сделать, когда попытки исчерпаны."""
        return self._on(Ending.FAILED, hook)

    def on_cancelled(self, hook: EndingHook[A]) -> EndingHook[A]:
        """Декоратор: что сделать, когда команда отменила задачу."""
        return self._on(Ending.CANCELLED, hook)

    def on_resolved(self, hook: EndingHook[A]) -> EndingHook[A]:
        """Декоратор: что сделать, когда команда отметила задачу решённой вручную."""
        return self._on(Ending.RESOLVED, hook)

    def handles(self, ending: Ending) -> bool:
        """Задан ли у вида задачи свой обработчик этого исхода."""
        return ending in self._hooks

    async def ended(
        self, ending: Ending, session: AsyncSession, task_id: int, raw_args: dict[str, JsonValue]
    ) -> None:
        hook = self._hooks.get(ending)
        if hook is not None:
            await hook(session, task_id, self._args_model.model_validate(raw_args))


def task[A: BaseModel](
    name: str, args_model: type[A], *, cancellable: bool = True, needs_attention: bool = True
) -> Callable[[Handler[A]], TaskDefinition[A]]:
    """Декоратор: превращает обработчик в вид задачи."""

    def decorate(handler: Handler[A]) -> TaskDefinition[A]:
        return TaskDefinition(
            name, args_model, handler, cancellable=cancellable, needs_attention=needs_attention
        )

    return decorate


class TaskRegistry:
    """Виды задач магазина по имени. Общий для воркера и действий команды."""

    def __init__(self, tasks: Iterable[RunnableTask]) -> None:
        self._tasks: dict[str, RunnableTask] = {}
        for definition in tasks:
            if definition.name in self._tasks:
                raise ValueError(f"Вид задачи {definition.name} зарегистрирован дважды")
            self._tasks[definition.name] = definition

    def get(self, name: str) -> RunnableTask | None:
        return self._tasks.get(name)

    def __iter__(self) -> Iterator[RunnableTask]:
        return iter(self._tasks.values())

    def cancellable(self, name: str) -> bool:
        # Вид, которого нет в этой версии кода, отменить можно: выполнить его уже некому
        definition = self.get(name)
        return definition is None or definition.cancellable


@dataclass(frozen=True)
class RetryPolicy:
    """Повторы с нарастающими интервалами (4.14).

    Повторы прекращаются по первому из условий: исчерпано число попыток круга или
    следующая попытка выпала бы за окно. Окно отсчитывается от первой засчитанной
    попытки круга, а после простоя панели — заново, от первой попытки после
    возвращения панели; число попыток при этом не сбрасывается (0047).
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
    """Повторить, отменить или решить вручную можно только окончательно проваленную задачу."""


class TaskNotCancellableError(Exception):
    """Эту задачу нельзя отменить — только повторить или отметить решённой вручную (4.31)."""


async def _lock_failed(session: AsyncSession, task_id: int) -> QueueTask:
    task = await session.scalar(
        select(QueueTask)
        .where(QueueTask.id == task_id, QueueTask.status == TaskStatus.FAILED)
        .with_for_update(key_share=True)
    )
    if task is None:
        raise TaskNotFailedError(f"Задача {task_id} не в состоянии «провалена»")
    return task


def _require_comment(comment: str) -> str:
    comment = comment.strip()
    if not comment:
        raise ValueError("Нужен комментарий: он попадает в журнал (4.31)")
    return comment


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


async def _finish_by_team(
    session: AsyncSession,
    tasks: TaskRegistry,
    task: QueueTask,
    ending: Ending,
    status: TaskStatus,
    *,
    actor: Actor,
    comment: str,
) -> None:
    await session.execute(
        update(QueueTask)
        .where(QueueTask.id == task.id)
        .values(status=status, finished_at=func.clock_timestamp())
    )
    await record(
        session,
        actor=actor,
        action=f"queue.{ending}",
        subject=task_subject(task.id),
        outcome=Outcome.SUCCESS,
        details={"comment": comment},
    )
    definition = tasks.get(task.name)
    if definition is not None:
        await definition.ended(ending, session, task.id, task.args)


async def cancel_failed(
    session: AsyncSession, tasks: TaskRegistry, task_id: int, *, actor: Actor, comment: str
) -> None:
    """«Отменить» с комментарием (4.31): задача не выполняется, следующие задачи ключа
    идут дальше (4.32). Последствия для клиента и данных — обработчик `on_cancelled`."""
    comment = _require_comment(comment)
    task = await _lock_failed(session, task_id)
    if not tasks.cancellable(task.name):
        raise TaskNotCancellableError(f"Задачу {task.name} нельзя отменить")
    await _finish_by_team(
        session, tasks, task, Ending.CANCELLED, TaskStatus.CANCELLED, actor=actor, comment=comment
    )


async def resolve_failed(
    session: AsyncSession, tasks: TaskRegistry, task_id: int, *, actor: Actor, comment: str
) -> None:
    """«Отметить решённым вручную» с комментарием (4.31): команда сделала всё сама;
    следующие задачи ключа идут дальше."""
    comment = _require_comment(comment)
    task = await _lock_failed(session, task_id)
    await _finish_by_team(
        session, tasks, task, Ending.RESOLVED, TaskStatus.RESOLVED, actor=actor, comment=comment
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


@dataclass(frozen=True)
class FailedTask:
    """Проваленная задача для «Требуют внимания» (4.27, 4.31)."""

    task_id: int
    name: str
    args: dict[str, JsonValue]
    key: str | None
    failed_at: datetime | None
    last_error: str | None
    # Сколько задач того же ключа ждут за ней (4.27)
    waiting_behind: int
    # «Отменить» доступна; иначе — только «Повторить» и «Отметить решённым вручную» (4.31)
    cancellable: bool


async def failed_tasks(session: AsyncSession, tasks: TaskRegistry) -> list[FailedTask]:
    """Все окончательно проваленные задачи, от старых к новым (4.31)."""
    behind = aliased(QueueTask)
    waiting = (
        select(func.count())
        .where(
            behind.key == QueueTask.key,
            behind.id > QueueTask.id,
            behind.status.in_(UNFINISHED),
        )
        .scalar_subquery()
    )
    last_attempt = (
        select(QueueAttempt.finished_at, QueueAttempt.error)
        .where(QueueAttempt.task_id == QueueTask.id)
        .order_by(QueueAttempt.id.desc())
        .limit(1)
        .lateral()
    )
    rows = await session.execute(
        select(
            QueueTask.id,
            QueueTask.name,
            QueueTask.args,
            QueueTask.key,
            last_attempt.c.finished_at,
            last_attempt.c.error,
            waiting,
        )
        .outerjoin(last_attempt, true())
        .where(QueueTask.status == TaskStatus.FAILED)
        .order_by(QueueTask.id)
    )
    return [
        FailedTask(
            task_id=task_id,
            name=name,
            args=args,
            key=key,
            failed_at=failed_at,
            last_error=error,
            waiting_behind=behind_count or 0,
            cancellable=tasks.cancellable(name),
        )
        for task_id, name, args, key, failed_at, error, behind_count in rows
    ]


async def tasks_for(
    session: AsyncSession,
    name: str,
    args: dict[str, JsonValue],
    *,
    status: TaskStatus | None = None,
) -> list[int]:
    """Задачи вида `name`, в аргументах которых есть `args` (например, задачи применения
    одного платежа), от ранних к поздним."""
    query = select(QueueTask.id).where(QueueTask.name == name, QueueTask.args.contains(args))
    if status is not None:
        query = query.where(QueueTask.status == status)
    return list(await session.scalars(query.order_by(QueueTask.id)))


async def unfinished_keys(session: AsyncSession, name: str, keys: Iterable[str]) -> set[str]:
    """Ключи, у которых задача вида `name` уже ждёт выполнения — чтобы не ставить её
    второй раз, пока первая не выполнена (например, сверку подписки)."""
    found = await session.scalars(
        select(QueueTask.key).where(
            QueueTask.name == name,
            QueueTask.key.in_(list(keys)),
            QueueTask.status.in_(UNFINISHED),
        )
    )
    return {key for key in found if key is not None}


async def waiting_panel_count(session: AsyncSession) -> int:
    """Сколько задач «ждут панель» — отдельная строка на главной, не в счётчике
    «Требуют внимания» (4.30)."""
    count = await session.scalar(
        select(func.count()).where(QueueTask.status == TaskStatus.WAITING_PANEL)
    )
    return count or 0


@dataclass(frozen=True)
class WaitingTask:
    """Задача «ждёт панель» — видна в «Требуют внимания» отдельно и продолжится сама (4.30)."""

    task_id: int
    name: str
    args: dict[str, JsonValue]
    key: str | None
    created_at: datetime


async def waiting_panel_tasks(session: AsyncSession) -> list[WaitingTask]:
    """Задачи, которые ждут восстановления панели, от старых к новым (4.30)."""
    result = await session.scalars(
        select(QueueTask).where(QueueTask.status == TaskStatus.WAITING_PANEL).order_by(QueueTask.id)
    )
    return [
        WaitingTask(
            task_id=task.id,
            name=task.name,
            args=task.args,
            key=task.key,
            created_at=task.created_at,
        )
        for task in result
    ]
