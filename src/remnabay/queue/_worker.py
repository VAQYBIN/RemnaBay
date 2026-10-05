"""Воркер очереди: выборка со SKIP LOCKED, выполнение, повторы (0024).

Как выполняется задача:
1. Транзакция воркера блокирует строку задачи (`FOR NO KEY UPDATE SKIP LOCKED`) и
   держит блокировку до конца. Если воркер упал, PostgreSQL закрывает соединение,
   блокировка снимается, и задачу берёт другой воркер.
2. Попытка засчитывается до выполнения отдельной короткой транзакцией (4.18):
   задача, которая раз за разом роняет процесс, доходит до лимита попыток.
3. Задача выполняется в точке сохранения той же транзакции воркера с таймаутом.
   Её изменения и отметка «выполнена» фиксируются одним коммитом.
4. При ошибке точка сохранения откатывается, текст ошибки сразу записывается
   отдельной транзакцией, затем назначается повтор или задача проваливается.

Таймаут задачи меньше `idle_in_transaction_session_timeout` соединения воркера:
иначе PostgreSQL рвал бы соединение у здоровой долгой задачи, и она
перезапускалась бы без счёта попыток (эксперимент с очередью, сценарий 5.4).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from remnabay.journal import Actor, JsonValue, Outcome, record
from remnabay.queue._core import (
    RetryPolicy,
    RunnableTask,
    TaskContext,
    TaskDefinition,
    task,
    task_subject,
)
from remnabay.queue._models import (
    RUNNABLE,
    UNFINISHED,
    AttemptResult,
    QueueAttempt,
    QueuePeriodicSlot,
    QueueTask,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# Выполненные и отменённые задачи хранятся 30 дней; проваленные и ждущие — пока
# их не разберут. Попытки остаются в журнале и после очистки.
FINISHED_RETENTION = timedelta(days=30)
SLOT_RETENTION = timedelta(days=1)
CLEANUP_EVERY = timedelta(hours=1)
ERROR_TEXT_LIMIT = 2000
ABORTED_ERROR = "Попытка оборвалась: процесс воркера остановился или потерял соединение с базой"


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 4
    poll_interval: timedelta = timedelta(milliseconds=500)
    # Запрос к панели внутри задачи — порядка 10 секунд (0036); задача целиком — дольше
    task_timeout: timedelta = timedelta(seconds=30)
    idle_in_transaction_timeout: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.task_timeout >= self.idle_in_transaction_timeout:
            raise ValueError(
                "Таймаут задачи должен быть меньше таймаута простоя транзакции: иначе "
                "PostgreSQL рвёт соединение у долгой задачи, и она перезапускается без "
                "счёта попыток"
            )

    @property
    def pool_size(self) -> int:
        """Соединений нужно по два на поток (задача и попытки) и одно планировщику."""
        return self.concurrency * 2 + 1


# Политика повторов, которая читается при каждом решении: настройки оператора
# меняются без перезапуска (04-operator-settings)
type PolicySource = Callable[[AsyncSession], Awaitable[RetryPolicy]]


class Scheduled(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def every(self) -> timedelta: ...

    async def enqueue(self, session: AsyncSession) -> int: ...


@dataclass(frozen=True)
class Periodic[A: BaseModel]:
    """Периодическая задача: ставится раз в `every`, даже если воркеров несколько."""

    task: TaskDefinition[A]
    args: A
    every: timedelta

    @property
    def name(self) -> str:
        return self.task.name

    async def enqueue(self, session: AsyncSession) -> int:
        return await self.task.enqueue(session, self.args)


class CleanupArgs(BaseModel):
    pass


@task("queue.cleanup", CleanupArgs)
async def cleanup(context: TaskContext, _args: CleanupArgs) -> None:
    """Удаляет старые выполненные и отменённые задачи и старые слоты расписания."""
    now = func.clock_timestamp()
    await context.session.execute(
        delete(QueueTask).where(
            QueueTask.status.in_((TaskStatus.DONE, TaskStatus.CANCELLED)),
            QueueTask.finished_at < now - FINISHED_RETENTION,
        )
    )
    # Последний слот задачи не удаляется: иначе задача с периодом больше суток
    # поставилась бы ещё раз в том же периоде
    later = aliased(QueuePeriodicSlot)
    await context.session.execute(
        delete(QueuePeriodicSlot).where(
            QueuePeriodicSlot.slot_start < now - SLOT_RETENTION,
            exists().where(
                later.name == QueuePeriodicSlot.name,
                later.slot_start > QueuePeriodicSlot.slot_start,
            ),
        )
    )


class TaskTimeoutError(Exception):
    """Задача не уложилась в свой таймаут.

    Отдельный класс, а не `TimeoutError`: таймаут запроса к панели означает «панель
    недоступна» (4.30), а таймаут самой задачи — обычная ошибка с подсчётом попыток.
    """

    def __init__(self, timeout: timedelta) -> None:
        super().__init__(f"Превышено время выполнения задачи ({timeout.total_seconds():g} с)")


class _BrokenTransactionError(Exception):
    """Транзакция воркера сломалась после ошибки задачи — повтор назначается заново.

    Так бывает, когда таймаут задачи прервал её SQL-запрос: SQLAlchemy считает
    соединение испорченным и не даёт продолжить транзакцию.
    """

    def __init__(self, task_id: int, retry_round: int, number: int, *, unavailable: bool) -> None:
        super().__init__(f"Транзакция воркера сломалась после ошибки задачи {task_id}")
        self.task_id = task_id
        self.retry_round = retry_round
        self.number = number
        self.unavailable = unavailable


def _describe(error: BaseException) -> str:
    if isinstance(error, TaskTimeoutError):
        return str(error)
    return f"{type(error).__name__}: {error}"[:ERROR_TEXT_LIMIT]


def _slot_start(now: datetime, every: timedelta) -> datetime:
    step = every.total_seconds()
    return datetime.fromtimestamp(now.timestamp() // step * step, tz=UTC)


async def _wait(stop: asyncio.Event, pause: timedelta) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=pause.total_seconds())


class Worker:
    def __init__(
        self,
        engine: AsyncEngine,
        tasks: Sequence[RunnableTask],
        *,
        periodic: Sequence[Scheduled] = (),
        policy: RetryPolicy | PolicySource | None = None,
        config: WorkerConfig | None = None,
        unavailable: tuple[type[Exception], ...] = (),
    ) -> None:
        """`policy` — постоянная политика повторов или функция, которая читает её из
        базы при каждом решении.

        `unavailable` — ошибки «внешний сервис недоступен»: задача с такой ошибкой
        ждёт («ждёт панель», 4.30), а не проваливается, и попытка не считается в лимит.
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._tasks: dict[str, RunnableTask] = {}
        for definition in (*tasks, cleanup):
            if definition.name in self._tasks:
                raise ValueError(f"Вид задачи {definition.name} зарегистрирован дважды")
            self._tasks[definition.name] = definition
        self._periodic = (*periodic, Periodic(cleanup, CleanupArgs(), CLEANUP_EVERY))
        self._policy = policy or RetryPolicy()
        self._config = config or WorkerConfig()
        self._unavailable = unavailable

    async def run(self, stop: asyncio.Event) -> None:
        """Работает, пока не выставлен `stop`; начатые задачи доделываются."""
        async with asyncio.TaskGroup() as group:
            for _ in range(self._config.concurrency):
                group.create_task(self._loop(stop, self.run_one))
            group.create_task(self._loop(stop, self._schedule_and_report))

    async def _schedule_and_report(self) -> bool:
        await self.schedule_periodic()
        return False

    async def _loop(self, stop: asyncio.Event, step: Callable[[], Awaitable[bool]]) -> None:
        while not stop.is_set():
            try:
                worked = await step()
            except Exception:
                logger.exception("Сбой цикла воркера")
                worked = False
            if not worked:
                await _wait(stop, self._config.poll_interval)

    async def schedule_periodic(self) -> None:
        """Ставит периодические задачи, чей слот ещё не занят."""
        async with self._sessions() as session, session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                return
            for periodic in self._periodic:
                inserted = await session.scalar(
                    insert(QueuePeriodicSlot)
                    .values(name=periodic.name, slot_start=_slot_start(now, periodic.every))
                    .on_conflict_do_nothing()
                    .returning(QueuePeriodicSlot.name)
                )
                if inserted is not None:
                    await periodic.enqueue(session)

    async def run_one(self) -> bool:
        """Берёт и выполняет одну задачу. `False` — брать сейчас нечего."""
        try:
            return await self._take_and_run()
        except _BrokenTransactionError as broken:
            await self._reschedule_after_broken(broken)
            return True

    async def _take_and_run(self) -> bool:
        async with self._sessions() as session, self._sessions() as side, session.begin():
            idle_ms = int(self._config.idle_in_transaction_timeout.total_seconds() * 1000)
            await session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {idle_ms}")
            )
            row = (await session.execute(self._next_task_query())).first()
            if row is None:
                return False
            task_id, name, args, retry_round = row
            await self._close_aborted_attempts(session, task_id)
            if await self._exhausted_before_start(session, task_id, retry_round):
                await self._fail(session, task_id)
                return True
            number = await self._start_attempt(side, task_id, retry_round)
            await self._execute(session, side, task_id, name, args, retry_round, number)
        return True

    @staticmethod
    def _next_task_query():
        earlier = aliased(QueueTask)
        return (
            select(QueueTask.id, QueueTask.name, QueueTask.args, QueueTask.retry_round)
            .where(
                QueueTask.status.in_(RUNNABLE),
                QueueTask.run_at <= func.now(),
                # Порядок по ключу: никакая более ранняя задача ключа не ждёт (4.17, 4.27)
                ~exists().where(
                    earlier.key == QueueTask.key,
                    earlier.id < QueueTask.id,
                    earlier.status.in_(UNFINISHED),
                ),
            )
            .order_by(QueueTask.id)
            .limit(1)
            .with_for_update(skip_locked=True, key_share=True, of=QueueTask)
        )

    async def _execute(
        self,
        session: AsyncSession,
        side: AsyncSession,
        task_id: int,
        name: str,
        args: dict[str, JsonValue],
        retry_round: int,
        number: int,
    ) -> None:
        context = TaskContext(session=session, task_id=task_id, attempt_number=number)
        try:
            async with session.begin_nested():
                await self._run_with_timeout(context, name, args)
        except self._unavailable as error:
            await self._finish_attempt(side, task_id, number, AttemptResult.UNAVAILABLE, error)
            await self._after_failure(session, task_id, retry_round, number, unavailable=True)
        except Exception as error:
            await self._finish_attempt(side, task_id, number, AttemptResult.ERROR, error)
            await self._after_failure(session, task_id, retry_round, number, unavailable=False)
        else:
            await session.execute(
                update(QueueAttempt)
                .where(QueueAttempt.task_id == task_id, QueueAttempt.number == number)
                .values(finished_at=func.clock_timestamp(), result=AttemptResult.DONE)
            )
            await self._set_status(
                session, task_id, TaskStatus.DONE, finished_at=func.clock_timestamp()
            )
            await record(
                session,
                actor=Actor.SYSTEM,
                action="queue.attempt",
                subject=task_subject(task_id),
                outcome=Outcome.SUCCESS,
                details={"attempt": number, "task": name},
            )

    async def _run_with_timeout(
        self, context: TaskContext, name: str, args: dict[str, JsonValue]
    ) -> None:
        timer = asyncio.timeout(self._config.task_timeout.total_seconds())
        try:
            async with timer:
                definition = self._tasks.get(name)
                if definition is None:
                    raise LookupError(f"Неизвестный вид задачи {name}")
                await definition.run(context, args)
        except TimeoutError as error:
            if timer.expired():
                raise TaskTimeoutError(self._config.task_timeout) from error
            raise

    async def _after_failure(
        self,
        session: AsyncSession,
        task_id: int,
        retry_round: int,
        number: int,
        *,
        unavailable: bool,
    ) -> None:
        try:
            await self._reschedule(session, task_id, retry_round, unavailable=unavailable)
        except SQLAlchemyError as error:
            raise _BrokenTransactionError(
                task_id, retry_round, number, unavailable=unavailable
            ) from error

    async def _reschedule(
        self, session: AsyncSession, task_id: int, retry_round: int, *, unavailable: bool
    ) -> None:
        """После неудачной попытки: «ждёт панель», повтор с паузой или провал."""
        if unavailable:
            policy = await self._policy_for(session)
            await self._set_status(
                session,
                task_id,
                TaskStatus.WAITING_PANEL,
                run_at=func.clock_timestamp() + policy.unavailable_recheck,
            )
        else:
            await self._retry_or_fail(session, task_id, retry_round)

    async def _reschedule_after_broken(self, broken: _BrokenTransactionError) -> None:
        """Назначает повтор в новой транзакции, если задачу ещё никто не взял заново."""
        async with self._sessions() as session, session.begin():
            locked = await session.scalar(
                select(QueueTask.id)
                .where(
                    QueueTask.id == broken.task_id,
                    QueueTask.status.in_(RUNNABLE),
                    QueueTask.retry_round == broken.retry_round,
                    ~exists().where(
                        QueueAttempt.task_id == QueueTask.id,
                        QueueAttempt.number > broken.number,
                    ),
                )
                .with_for_update(skip_locked=True, key_share=True, of=QueueTask)
            )
            if locked is None:
                return
            await self._reschedule(
                session, broken.task_id, broken.retry_round, unavailable=broken.unavailable
            )

    async def _start_attempt(self, side: AsyncSession, task_id: int, retry_round: int) -> int:
        """Засчитывает попытку до выполнения — отдельной транзакцией (4.18)."""
        async with side.begin():
            previous = await side.scalar(
                select(func.count()).where(QueueAttempt.task_id == task_id)
            )
            number = (previous or 0) + 1
            await side.execute(
                insert(QueueAttempt).values(task_id=task_id, number=number, retry_round=retry_round)
            )
        return number

    async def _finish_attempt(
        self,
        side: AsyncSession,
        task_id: int,
        number: int,
        result: AttemptResult,
        error: Exception,
    ) -> None:
        """Записывает ошибку попытки сразу и отдельно: текст не потеряется, даже если
        транзакция воркера уже неработоспособна (например, после таймаута)."""
        error_text = _describe(error)
        logger.warning("Задача %s, попытка %s: %s", task_id, number, error_text)
        async with side.begin():
            await side.execute(
                update(QueueAttempt)
                .where(
                    QueueAttempt.task_id == task_id,
                    QueueAttempt.number == number,
                    QueueAttempt.finished_at.is_(None),
                )
                .values(finished_at=func.clock_timestamp(), result=result, error=error_text)
            )
            await record(
                side,
                actor=Actor.SYSTEM,
                action="queue.attempt",
                subject=task_subject(task_id),
                outcome=Outcome.FAILURE,
                details={"attempt": number, "result": result.value, "error": error_text},
            )

    async def _close_aborted_attempts(self, session: AsyncSession, task_id: int) -> None:
        """Попытки без окончания — оборвались вместе с прежним воркером."""
        aborted = await session.scalars(
            update(QueueAttempt)
            .where(QueueAttempt.task_id == task_id, QueueAttempt.finished_at.is_(None))
            .values(
                finished_at=func.clock_timestamp(),
                result=AttemptResult.ABORTED,
                error=ABORTED_ERROR,
            )
            .returning(QueueAttempt.number)
        )
        for number in aborted.all():
            await record(
                session,
                actor=Actor.SYSTEM,
                action="queue.attempt",
                subject=task_subject(task_id),
                outcome=Outcome.FAILURE,
                details={
                    "attempt": number,
                    "result": AttemptResult.ABORTED.value,
                    "error": ABORTED_ERROR,
                },
            )

    async def _policy_for(self, session: AsyncSession) -> RetryPolicy:
        if isinstance(self._policy, RetryPolicy):
            return self._policy
        return await self._policy(session)

    async def _round_stats(
        self, session: AsyncSession, task_id: int, retry_round: int
    ) -> tuple[int, datetime | None, datetime]:
        """Сколько попыток круга засчитано, когда была первая и сколько сейчас времени."""
        counted = QueueAttempt.result.is_distinct_from(AttemptResult.UNAVAILABLE)
        row = (
            await session.execute(
                select(
                    func.count().filter(counted),
                    func.min(QueueAttempt.started_at).filter(counted),
                    func.clock_timestamp(),
                ).where(QueueAttempt.task_id == task_id, QueueAttempt.retry_round == retry_round)
            )
        ).one()
        return row[0], row[1], row[2]

    async def _exhausted_before_start(
        self, session: AsyncSession, task_id: int, retry_round: int
    ) -> bool:
        policy = await self._policy_for(session)
        counted, first_started, now = await self._round_stats(session, task_id, retry_round)
        if counted >= policy.max_attempts:
            return True
        return first_started is not None and now - first_started >= policy.max_age

    async def _retry_or_fail(self, session: AsyncSession, task_id: int, retry_round: int) -> None:
        policy = await self._policy_for(session)
        counted, first_started, now = await self._round_stats(session, task_id, retry_round)
        delay = policy.delay_after(counted)
        out_of_window = first_started is not None and now + delay - first_started > policy.max_age
        if counted >= policy.max_attempts or out_of_window:
            await self._fail(session, task_id)
        else:
            await self._set_status(
                session, task_id, TaskStatus.PENDING, run_at=func.clock_timestamp() + delay
            )

    async def _fail(self, session: AsyncSession, task_id: int) -> None:
        """Окончательный провал: задача ждёт команду, следующие задачи ключа — её (4.27)."""
        await self._set_status(session, task_id, TaskStatus.FAILED)
        await record(
            session,
            actor=Actor.SYSTEM,
            action="queue.failed",
            subject=task_subject(task_id),
            outcome=Outcome.FAILURE,
        )

    @staticmethod
    async def _set_status(
        session: AsyncSession, task_id: int, status: TaskStatus, **values: object
    ) -> None:
        await session.execute(
            update(QueueTask).where(QueueTask.id == task_id).values(status=status, **values)
        )
