"""Задачи и запуск воркера для тестов очереди.

Модуль запускается и отдельным процессом (`python -m tests.queue_support`), чтобы
проверять падение воркера: kill -9 и задачу, которая роняет процесс.
"""

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from remnabay.queue import (
    FailedFallback,
    Periodic,
    PolicySource,
    RejectedError,
    RetryPolicy,
    TaskContext,
    TaskDefinition,
    TaskRegistry,
    TaskStatus,
    Worker,
    WorkerConfig,
    task,
    task_status,
)

DATABASE_URL_ENV = "QUEUE_TEST_DATABASE_URL"

# Побочные эффекты задач пишутся в отдельную схему: Alembic её не видит,
# и проверка «схема совпадает с моделями» не ломается
PREPARE_SQL = (
    "CREATE SCHEMA IF NOT EXISTS queue_test",
    """
    CREATE TABLE IF NOT EXISTS queue_test.events (
        id bigserial PRIMARY KEY,
        task_id bigint NOT NULL,
        label text NOT NULL,
        attempt int NOT NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz NOT NULL,
        pid int NOT NULL
    )
    """,
)
CLEAN_SQL = (
    "TRUNCATE queue_attempts, queue_tasks, queue_keys, queue_periodic_slots, queue_test.events"
)

# Состояние «панели» и «переключателей сбоя» для задач в этом процессе
PANEL = {"up": True}
FAILING: set[str] = set()


class PanelDownError(Exception):
    """Подставная «панель недоступна» (4.30)."""


async def _write_event(context: TaskContext, label: str, started_at: datetime) -> None:
    await _insert_event(context.session, context.task_id, label, context.attempt_number, started_at)


async def _insert_event(
    session: AsyncSession, task_id: int, label: str, attempt: int, started_at: datetime
) -> None:
    await session.execute(
        text(
            "INSERT INTO queue_test.events (task_id, label, attempt, started_at, finished_at, pid)"
            " VALUES (:task_id, :label, :attempt, :started_at, clock_timestamp(), :pid)"
        ),
        {
            "task_id": task_id,
            "label": label,
            "attempt": attempt,
            "started_at": started_at,
            "pid": os.getpid(),
        },
    )


class RecordArgs(BaseModel):
    label: str
    sleep: float = 0
    # Пауза только в первой попытке: чтобы убить воркер посреди задачи
    first_attempt_sleep: float = 0


@task("test.record", RecordArgs)
async def record_event(context: TaskContext, args: RecordArgs) -> None:
    started_at = datetime.now(UTC)
    pause = args.first_attempt_sleep if context.attempt_number == 1 else 0
    await asyncio.sleep(args.sleep + pause)
    await _write_event(context, args.label, started_at)


class FlakyArgs(BaseModel):
    label: str
    fail_times: int
    sleep: float = 0


@task("test.flaky", FlakyArgs)
async def flaky(context: TaskContext, args: FlakyArgs) -> None:
    started_at = datetime.now(UTC)
    await asyncio.sleep(args.sleep)
    if context.attempt_number <= args.fail_times:
        raise RuntimeError(f"сбой попытки {context.attempt_number}")
    await _write_event(context, args.label, started_at)


class LabelArgs(BaseModel):
    label: str


@task("test.switch", LabelArgs)
async def switch(context: TaskContext, args: LabelArgs) -> None:
    """Падает, пока метка в `FAILING`."""
    if args.label in FAILING:
        raise RuntimeError(f"{args.label}: сбой")
    await _write_event(context, args.label, datetime.now(UTC))


@task("test.hooked", LabelArgs)
async def hooked(context: TaskContext, args: LabelArgs) -> None:
    """Как `switch`, но исходы оставляют событие `failed:`, `cancelled:`, `resolved:`."""
    if args.label in FAILING:
        raise RuntimeError(f"{args.label}: сбой")
    await _write_event(context, args.label, datetime.now(UTC))


@hooked.on_failed
async def _hooked_failed(session: AsyncSession, task_id: int, args: LabelArgs) -> None:
    await _insert_event(session, task_id, f"failed:{args.label}", 0, datetime.now(UTC))


@hooked.on_cancelled
async def _hooked_cancelled(session: AsyncSession, task_id: int, args: LabelArgs) -> None:
    await _insert_event(session, task_id, f"cancelled:{args.label}", 0, datetime.now(UTC))


@hooked.on_resolved
async def _hooked_resolved(session: AsyncSession, task_id: int, args: LabelArgs) -> None:
    await _insert_event(session, task_id, f"resolved:{args.label}", 0, datetime.now(UTC))


@task("test.irreversible", LabelArgs, cancellable=False)
async def irreversible(context: TaskContext, args: LabelArgs) -> None:
    """Как смена даты при возврате: отменить нельзя (4.31)."""
    if args.label in FAILING:
        raise RuntimeError(f"{args.label}: сбой")
    await _write_event(context, args.label, datetime.now(UTC))


@task("test.bad_hook", LabelArgs)
async def bad_hook(_context: TaskContext, args: LabelArgs) -> None:
    raise RuntimeError(f"{args.label}: сбой")


@bad_hook.on_failed
async def _bad_hook_failed(_session: AsyncSession, _task_id: int, _args: LabelArgs) -> None:
    raise RuntimeError("обработчик провала сломан")


@task("test.panel_switch", LabelArgs)
async def panel_switch(context: TaskContext, args: LabelArgs) -> None:
    """Панель недоступна — «ждёт панель»; иначе падает, пока метка в `FAILING`."""
    if not PANEL["up"]:
        raise PanelDownError("панель недоступна")
    if args.label in FAILING:
        raise RuntimeError(f"{args.label}: сбой")
    await _write_event(context, args.label, datetime.now(UTC))


@task("test.rejecting", LabelArgs)
async def rejecting(context: TaskContext, args: LabelArgs) -> None:
    """Внешний сервис отказывает, пока метка в `FAILING`; событие — с итогом прошлой попытки."""
    previous = context.previous_result.value if context.previous_result else "-"
    await _write_event(context, f"{args.label}:{previous}", datetime.now(UTC))
    if args.label in FAILING:
        raise RejectedError("сервис отказал")


@task("test.panel", LabelArgs)
async def needs_panel(context: TaskContext, args: LabelArgs) -> None:
    if not PANEL["up"]:
        raise PanelDownError("панель недоступна")
    await _write_event(context, args.label, datetime.now(UTC))


class SqlSleepArgs(BaseModel):
    seconds: float


@task("test.sql_sleep", SqlSleepArgs)
async def sql_sleep(context: TaskContext, args: SqlSleepArgs) -> None:
    """Долгий SQL-запрос: таймаут задачи прерывает его посреди выполнения."""
    await context.session.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": args.seconds})


class NoArgs(BaseModel):
    pass


@task("test.crash", NoArgs)
async def crash(_context: TaskContext, _args: NoArgs) -> None:
    """Роняет процесс воркера, как нехватка памяти."""
    os._exit(1)


class SpawnArgs(BaseModel):
    labels: list[str]
    fail: bool = False


@task("test.spawn", SpawnArgs)
async def spawn(context: TaskContext, args: SpawnArgs) -> None:
    """Большая работа делится на небольшие задачи (4.28)."""
    for label in args.labels:
        await record_event.enqueue(context.session, RecordArgs(label=label))
    if args.fail:
        raise RuntimeError("сбой после постановки подзадач")


ALL_TASKS = (
    record_event,
    flaky,
    switch,
    hooked,
    irreversible,
    bad_hook,
    panel_switch,
    rejecting,
    needs_panel,
    crash,
    spawn,
    sql_sleep,
)
REGISTRY = TaskRegistry(ALL_TASKS)

FAST_POLICY = RetryPolicy(
    first_delay=timedelta(milliseconds=50),
    max_delay=timedelta(milliseconds=200),
    max_attempts=4,
    max_age=timedelta(seconds=60),
    unavailable_recheck=timedelta(milliseconds=50),
)
FAST_CONFIG = WorkerConfig(
    concurrency=3,
    poll_interval=timedelta(milliseconds=20),
    task_timeout=timedelta(seconds=5),
    idle_in_transaction_timeout=timedelta(seconds=10),
)


def make_worker(
    engine: AsyncEngine,
    *,
    policy: RetryPolicy | PolicySource = FAST_POLICY,
    config: WorkerConfig = FAST_CONFIG,
    periodic: Sequence[Periodic[RecordArgs]] = (),
    unavailable: tuple[type[Exception], ...] = (PanelDownError,),
    on_failed: FailedFallback | None = None,
) -> Worker:
    return Worker(
        engine,
        ALL_TASKS,
        periodic=periodic,
        policy=policy,
        config=config,
        unavailable=unavailable,
        on_failed=on_failed,
    )


async def prepare(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in PREPARE_SQL:
            await connection.execute(text(statement))
        await connection.execute(text(CLEAN_SQL))


async def enqueue[A: BaseModel](
    engine: AsyncEngine, definition: TaskDefinition[A], args: A, *, key: str | None = None
) -> int:
    async with AsyncSession(engine) as session, session.begin():
        return await definition.enqueue(session, args, key=key)


async def status_of(engine: AsyncEngine, task_id: int) -> TaskStatus | None:
    async with AsyncSession(engine) as session:
        return await task_status(session, task_id)


async def events(engine: AsyncEngine) -> list[tuple[int, str, int, datetime, datetime, int]]:
    """События задач: (task_id, label, attempt, started_at, finished_at, pid) по порядку записи."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT task_id, label, attempt, started_at, finished_at, pid"
                " FROM queue_test.events ORDER BY id"
            )
        )
        return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


async def wait_until(condition: Callable[[], Awaitable[bool]], limit_seconds: float = 15) -> None:
    """Опрашивает базу, пока не выполнится условие; дольше предела — ошибка теста."""
    async with asyncio.timeout(limit_seconds):
        while True:
            if await condition():
                return
            await asyncio.sleep(0.02)


async def run_workers(
    workers: Sequence[Worker],
    until: Callable[[], Awaitable[bool]],
    limit_seconds: float = 15,
) -> None:
    """Запускает воркеры и останавливает их, когда выполнится условие."""
    stop = asyncio.Event()
    runners = [asyncio.create_task(worker.run(stop)) for worker in workers]
    try:
        await wait_until(until, limit_seconds)
    finally:
        stop.set()
        await asyncio.gather(*runners)


async def all_finished(engine: AsyncEngine, task_ids: Sequence[int]) -> bool:
    statuses = [await status_of(engine, task_id) for task_id in task_ids]
    return all(s in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED) for s in statuses)


async def _main(max_attempts: int, exit_when_idle: bool) -> None:
    engine = create_async_engine(os.environ[DATABASE_URL_ENV], pool_size=4)
    policy = RetryPolicy(
        first_delay=timedelta(milliseconds=10),
        max_delay=timedelta(milliseconds=10),
        max_attempts=max_attempts,
    )
    worker = make_worker(engine, policy=policy)
    idle_polls = 0
    try:
        while idle_polls < 10 or not exit_when_idle:
            worked = await worker.run_one()
            idle_polls = 0 if worked else idle_polls + 1
            if not worked:
                await asyncio.sleep(0.02)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--exit-when-idle", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(_main(arguments.max_attempts, arguments.exit_when_idle))
