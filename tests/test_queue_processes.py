"""Очередь при падении процесса воркера: kill -9 и задача, которая роняет процесс."""

import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from remnabay.journal import entries_for
from remnabay.queue import AttemptResult, TaskStatus, attempts_of, task_subject
from tests.queue_support import (
    DATABASE_URL_ENV,
    NoArgs,
    RecordArgs,
    all_finished,
    crash,
    enqueue,
    events,
    make_worker,
    record_event,
    run_workers,
    status_of,
    wait_until,
)


async def _start_worker_process(database_url: str, *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.queue_support",
        *args,
        env={**os.environ, DATABASE_URL_ENV: database_url},
    )


async def _attempt_started(engine: AsyncEngine, task_id: int) -> bool:
    async with AsyncSession(engine) as session:
        return bool(await attempts_of(session, task_id))


async def test_4_16_worker_killed_mid_task_operation_is_done_by_another_worker(
    queue_engine: AsyncEngine, database_url: str
) -> None:
    """4.16: воркер убит (kill -9) посреди операции — операция не теряется: её берёт
    другой воркер; оборванная попытка видна в истории и журнале."""
    task_id = await enqueue(
        queue_engine, record_event, RecordArgs(label="k", first_attempt_sleep=30), key="sub:k"
    )
    process = await _start_worker_process(database_url)
    await wait_until(lambda: _attempt_started(queue_engine, task_id))

    process.kill()
    await process.wait()
    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [task_id]))

    assert await status_of(queue_engine, task_id) == TaskStatus.DONE
    assert [(e[1], e[2]) for e in await events(queue_engine)] == [("k", 2)]
    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
        journal = await entries_for(session, task_subject(task_id))
    assert [a.result for a in attempts] == [AttemptResult.ABORTED, AttemptResult.DONE]
    assert journal[0].details["result"] == "aborted"


async def test_4_18_task_crashing_worker_process_reaches_attempt_limit(
    queue_engine: AsyncEngine, database_url: str
) -> None:
    """4.18: попытка засчитывается до выполнения — операция, которая раз за разом роняет
    процесс, доходит до лимита попыток и становится проваленной, а не перезапускается
    бесконечно."""
    task_id = await enqueue(queue_engine, crash, NoArgs())

    exit_codes: list[int] = []
    for _ in range(6):
        process = await _start_worker_process(
            database_url, "--max-attempts", "3", "--exit-when-idle"
        )
        exit_codes.append(await asyncio.wait_for(process.wait(), timeout=30))
        if await status_of(queue_engine, task_id) == TaskStatus.FAILED:
            break

    # Три попытки уронили процесс; четвёртый запуск видит исчерпанный лимит
    assert exit_codes == [1, 1, 1, 0]
    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
        journal = await entries_for(session, task_subject(task_id))
    assert [a.result for a in attempts] == [AttemptResult.ABORTED] * 3
    assert journal[-1].action == "queue.failed"
