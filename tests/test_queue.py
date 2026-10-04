"""Очередь задач (0024): сценарии эксперимента и доработки решения.

Критерии блока 4 здесь проверяются как механизм очереди: 4.14, 4.16, 4.17, 4.18,
4.27, 4.28, 4.30. Экран «Требуют внимания» и уведомления команде — блок 4.
"""

import asyncio
import itertools
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from remnabay.journal import Actor, entries_for
from remnabay.queue import (
    AttemptResult,
    Periodic,
    RetryPolicy,
    TaskNotFailedError,
    TaskStatus,
    WorkerConfig,
    attempts_of,
    cancel_failed,
    retry_failed,
    task_subject,
    waiting_behind,
)
from remnabay.queue._models import QueueTask
from tests.queue_support import (
    FAILING,
    FAST_CONFIG,
    FAST_POLICY,
    PANEL,
    FlakyArgs,
    LabelArgs,
    RecordArgs,
    SpawnArgs,
    all_finished,
    enqueue,
    events,
    flaky,
    make_worker,
    needs_panel,
    record_event,
    run_workers,
    spawn,
    status_of,
    switch,
)


async def _count_tasks(engine: AsyncEngine) -> int:
    """Задачи тестов — без встроенной очистки, которую ставит планировщик воркера."""
    async with AsyncSession(engine) as session:
        count = await session.scalar(select(func.count()).where(QueueTask.name != "queue.cleanup"))
        return count or 0


# --- Постановка ---


async def test_enqueue_is_committed_with_callers_transaction(queue_engine: AsyncEngine) -> None:
    """Задача ставится в транзакции вызывающего кода: коммит — задача есть."""
    async with AsyncSession(queue_engine) as session, session.begin():
        await record_event.enqueue(session, RecordArgs(label="a"))

    assert await _count_tasks(queue_engine) == 1


async def test_enqueue_is_rolled_back_with_callers_transaction(queue_engine: AsyncEngine) -> None:
    """Откат транзакции вызывающего кода отменяет и задачу."""
    async with AsyncSession(queue_engine) as session, session.begin():
        await record_event.enqueue(session, RecordArgs(label="a"))
        await session.rollback()

    assert await _count_tasks(queue_engine) == 0


# --- Порядок (4.17) ---


async def _enqueue_keyed_series(engine: AsyncEngine, *, keyed: bool) -> list[int]:
    """Три подписки по пять операций; вторая операция подписки A дважды падает.

    Ранние операции длиннее поздних: без ключа поздние обогнали бы ранние.
    """
    task_ids: list[int] = []
    for subscription in ("A", "B", "C"):
        key = f"subscription:{subscription}" if keyed else None
        for seq in range(5):
            label = f"{subscription}{seq}"
            sleep = 0.05 - seq * 0.01
            if subscription == "A" and seq == 1:
                args = FlakyArgs(label=label, fail_times=2, sleep=sleep)
                task_ids.append(await enqueue(engine, flaky, args, key=key))
            else:
                args = RecordArgs(label=label, sleep=sleep)
                task_ids.append(await enqueue(engine, record_event, args, key=key))
    return task_ids


async def test_4_17_operations_of_one_subscription_run_strictly_in_order(
    queue_engine: AsyncEngine,
) -> None:
    """4.17: операции одной подписки выполняются по порядку и не пересекаются,
    даже при двух воркерах и повторе одной из операций."""
    task_ids = await _enqueue_keyed_series(queue_engine, keyed=True)
    workers = [make_worker(queue_engine), make_worker(queue_engine)]

    await run_workers(workers, lambda: all_finished(queue_engine, task_ids))

    done = await events(queue_engine)
    for subscription in ("A", "B", "C"):
        series = [e for e in done if e[1].startswith(subscription)]
        assert [e[1] for e in series] == [f"{subscription}{seq}" for seq in range(5)]
        for previous, following in itertools.pairwise(series):
            assert previous[4] <= following[3], "операции одной подписки пересеклись"


async def test_4_17_control_without_key_order_is_not_kept(queue_engine: AsyncEngine) -> None:
    """Контроль чувствительности: без ключа пять операций стартуют вместе и ранние
    (более долгие) завершаются последними — проверка порядка это поймала бы."""
    task_ids = [
        await enqueue(
            queue_engine, record_event, RecordArgs(label=f"n{seq}", sleep=0.25 - seq * 0.05)
        )
        for seq in range(5)
    ]
    config = WorkerConfig(
        concurrency=5,
        poll_interval=timedelta(milliseconds=20),
        task_timeout=timedelta(seconds=5),
        idle_in_transaction_timeout=timedelta(seconds=10),
    )

    await run_workers(
        [make_worker(queue_engine, config=config)], lambda: all_finished(queue_engine, task_ids)
    )

    assert [e[1] for e in await events(queue_engine)] != [f"n{seq}" for seq in range(5)]


async def test_4_17_concurrent_enqueue_for_one_key_waits_for_first_commit(
    queue_engine: AsyncEngine,
) -> None:
    """4.17: две транзакции ставят операции одной подписке одновременно — вторая ждёт
    коммита первой, поэтому порядок номеров совпадает с порядком коммитов. Другой
    ключ при этом не ждёт."""
    async with AsyncSession(queue_engine) as first, first.begin():
        first_id = await record_event.enqueue(first, RecordArgs(label="first"), key="sub:1")

        second = asyncio.create_task(
            enqueue(queue_engine, record_event, RecordArgs(label="second"), key="sub:1")
        )
        other_key = await asyncio.wait_for(
            enqueue(queue_engine, record_event, RecordArgs(label="other"), key="sub:2"), timeout=2
        )
        await asyncio.sleep(0.3)
        assert not second.done(), "вторая постановка не дождалась первой транзакции"

    second_id = await asyncio.wait_for(second, timeout=2)
    assert first_id < second_id
    assert other_key > first_id


# --- Повторы (4.14, 4.18) ---


async def test_4_14_retries_with_growing_intervals_until_attempt_limit(
    queue_engine: AsyncEngine,
) -> None:
    """4.14: повторы с нарастающими интервалами; после лимита попыток — «провалена»."""
    FAILING.add("x")
    policy = RetryPolicy(
        first_delay=timedelta(milliseconds=300),
        max_delay=timedelta(seconds=5),
        max_attempts=4,
        max_age=timedelta(seconds=60),
    )
    task_id = await enqueue(queue_engine, switch, LabelArgs(label="x"))

    await run_workers(
        [make_worker(queue_engine, policy=policy)],
        lambda: all_finished(queue_engine, [task_id]),
    )

    assert await status_of(queue_engine, task_id) == TaskStatus.FAILED
    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
    assert [a.result for a in attempts] == [AttemptResult.ERROR] * 4
    gaps = [
        (following.started_at - previous.started_at).total_seconds()
        for previous, following in itertools.pairwise(attempts)
    ]
    # Попытка не начинается раньше срока; опоздание — на опрос и задержки окружения.
    # Запас меньше шага, поэтому проверка заодно подтверждает, что интервалы растут
    for gap, expected in zip(gaps, (0.3, 0.6, 1.2), strict=True):
        assert expected <= gap < expected + 0.3


async def test_4_14_retries_stop_when_time_window_ends(queue_engine: AsyncEngine) -> None:
    """4.14: повторы прекращаются, когда следующая попытка выпала бы за окно по времени."""
    FAILING.add("x")
    policy = RetryPolicy(
        first_delay=timedelta(milliseconds=100),
        max_delay=timedelta(seconds=1),
        max_attempts=100,
        max_age=timedelta(milliseconds=500),
    )
    task_id = await enqueue(queue_engine, switch, LabelArgs(label="x"))

    await run_workers(
        [make_worker(queue_engine, policy=policy)],
        lambda: all_finished(queue_engine, [task_id]),
    )

    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
    # Попытки на 0; 0,1; 0,3 с — следующая была бы на 0,7 с, за окном 0,5 с
    assert len(attempts) == 3
    assert await status_of(queue_engine, task_id) == TaskStatus.FAILED


async def test_4_18_every_attempt_is_journaled_with_error_text(queue_engine: AsyncEngine) -> None:
    """4.18, 4.21: каждая попытка — в истории попыток и в журнале, с текстом ошибки."""
    task_id = await enqueue(queue_engine, flaky, FlakyArgs(label="x", fail_times=2))

    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [task_id]))

    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
        journal = await entries_for(session, task_subject(task_id))
    assert [(a.number, a.result, a.error) for a in attempts] == [
        (1, AttemptResult.ERROR, "RuntimeError: сбой попытки 1"),
        (2, AttemptResult.ERROR, "RuntimeError: сбой попытки 2"),
        (3, AttemptResult.DONE, None),
    ]
    assert [(e.action, e.details.get("attempt")) for e in journal] == [
        ("queue.attempt", 1),
        ("queue.attempt", 2),
        ("queue.attempt", 3),
    ]
    assert journal[0].details["error"] == "RuntimeError: сбой попытки 1"
    assert all(e.actor == Actor.SYSTEM for e in journal)


async def test_task_timeout_fails_the_attempt_and_is_counted(queue_engine: AsyncEngine) -> None:
    """Таймаут задачи (0024): слишком долгая задача падает как обычная ошибка, попытка
    засчитывается, и задача не перезапускается бесконечно."""
    config = WorkerConfig(
        concurrency=1,
        poll_interval=timedelta(milliseconds=20),
        task_timeout=timedelta(milliseconds=200),
        idle_in_transaction_timeout=timedelta(seconds=2),
    )
    policy = RetryPolicy(
        first_delay=timedelta(milliseconds=10), max_delay=timedelta(milliseconds=10), max_attempts=2
    )
    task_id = await enqueue(queue_engine, record_event, RecordArgs(label="slow", sleep=5))

    await run_workers(
        [make_worker(queue_engine, policy=policy, config=config)],
        lambda: all_finished(queue_engine, [task_id]),
    )

    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
    assert [a.error for a in attempts] == ["Превышено время выполнения задачи (0.2 с)"] * 2
    assert await status_of(queue_engine, task_id) == TaskStatus.FAILED


def test_task_timeout_must_be_shorter_than_idle_transaction_timeout() -> None:
    """Соотношение таймаутов проверяется при запуске: иначе бесконечный перезапуск (0024)."""
    with pytest.raises(ValueError, match="меньше таймаута простоя"):
        WorkerConfig(
            task_timeout=timedelta(seconds=60), idle_in_transaction_timeout=timedelta(seconds=60)
        )


# --- Ожидание после провала (4.27) и разбор командой ---


async def _fail_first_of_key(engine: AsyncEngine) -> tuple[int, int, int]:
    """Первая операция подписки проваливается; вторая ждёт; у другой подписки — своя очередь."""
    FAILING.add("k1")
    failed = await enqueue(engine, switch, LabelArgs(label="k1"), key="sub:k")
    behind = await enqueue(engine, record_event, RecordArgs(label="k2"), key="sub:k")
    other = await enqueue(engine, record_event, RecordArgs(label="l1"), key="sub:l")

    async def failed_and_other_done() -> bool:
        return (
            await status_of(engine, failed) == TaskStatus.FAILED
            and await status_of(engine, other) == TaskStatus.DONE
        )

    await run_workers([make_worker(engine)], failed_and_other_done)
    return failed, behind, other


async def test_4_27_failed_operation_holds_following_operations_of_subscription(
    queue_engine: AsyncEngine,
) -> None:
    """4.27: после окончательного провала следующие операции подписки не выполняются;
    видно, сколько их ждёт; операции других подписок выполняются."""
    failed, behind, _other = await _fail_first_of_key(queue_engine)

    # Воркер ещё поработал — ждущая операция так и не началась
    await run_workers([make_worker(queue_engine)], lambda: _sleep_true(0.3))
    assert await status_of(queue_engine, behind) == TaskStatus.PENDING
    assert [e[1] for e in await events(queue_engine)] == ["l1"]
    async with AsyncSession(queue_engine) as session:
        assert await waiting_behind(session, failed) == 1


async def _sleep_true(seconds: float) -> bool:
    await asyncio.sleep(seconds)
    return True


async def test_4_27_retry_by_team_runs_failed_operation_then_following(
    queue_engine: AsyncEngine,
) -> None:
    """4.27, 4.31: «Повторить» — проваленная операция выполняется заново, затем следующие."""
    failed, behind, _other = await _fail_first_of_key(queue_engine)
    FAILING.discard("k1")

    async with AsyncSession(queue_engine) as session, session.begin():
        await retry_failed(session, failed, actor=Actor.team_member(7))
    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [behind]))

    assert await status_of(queue_engine, failed) == TaskStatus.DONE
    assert [e[1] for e in await events(queue_engine)] == ["l1", "k1", "k2"]
    async with AsyncSession(queue_engine) as session:
        actions = [(e.action, e.actor) for e in await entries_for(session, task_subject(failed))]
    assert ("queue.retried", Actor.team_member(7)) in actions


async def test_4_32_cancel_by_team_releases_following_operations(
    queue_engine: AsyncEngine,
) -> None:
    """4.32: «Отменить» снимает ожидание; отмена с комментарием попадает в журнал."""
    failed, behind, _other = await _fail_first_of_key(queue_engine)

    async with AsyncSession(queue_engine) as session, session.begin():
        await cancel_failed(session, failed, actor=Actor.team_member(7), comment="клиент передумал")
    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [behind]))

    assert await status_of(queue_engine, failed) == TaskStatus.CANCELLED
    assert await status_of(queue_engine, behind) == TaskStatus.DONE
    async with AsyncSession(queue_engine) as session:
        journal = await entries_for(session, task_subject(failed))
    assert journal[-1].action == "queue.cancelled"
    assert journal[-1].details == {"comment": "клиент передумал"}


async def test_retry_and_cancel_only_for_failed_operation(queue_engine: AsyncEngine) -> None:
    """Повторить или отменить можно только окончательно проваленную операцию."""
    task_id = await enqueue(queue_engine, record_event, RecordArgs(label="a"))

    async with AsyncSession(queue_engine) as session, session.begin():
        with pytest.raises(TaskNotFailedError):
            await retry_failed(session, task_id, actor=Actor.team_member(7))
        with pytest.raises(TaskNotFailedError):
            await cancel_failed(session, task_id, actor=Actor.team_member(7), comment="—")


# --- «Ждёт панель» (4.30) ---


async def test_4_30_panel_unavailable_waits_without_failing_and_resumes(
    queue_engine: AsyncEngine,
) -> None:
    """4.30: пока панель недоступна, операция «ждёт панель», не проваливается и не тратит
    лимит попыток; следующие операции подписки ждут; когда панель вернулась — продолжает."""
    PANEL["up"] = False
    waiting = await enqueue(queue_engine, needs_panel, LabelArgs(label="p1"), key="sub:p")
    behind = await enqueue(queue_engine, record_event, RecordArgs(label="p2"), key="sub:p")

    async def tried_beyond_limit() -> bool:
        async with AsyncSession(queue_engine) as session:
            return len(await attempts_of(session, waiting)) > FAST_POLICY.max_attempts + 1

    await run_workers([make_worker(queue_engine)], tried_beyond_limit)
    assert await status_of(queue_engine, waiting) == TaskStatus.WAITING_PANEL
    assert await status_of(queue_engine, behind) == TaskStatus.PENDING

    PANEL["up"] = True
    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [behind]))
    assert [e[1] for e in await events(queue_engine)] == ["p1", "p2"]


# --- Большая работа (4.28) ---


async def test_4_28_task_enqueues_small_tasks_in_its_transaction(queue_engine: AsyncEngine) -> None:
    """4.28: задача ставит много небольших задач; они выполняются."""
    parent = await enqueue(queue_engine, spawn, SpawnArgs(labels=["s1", "s2", "s3"]))

    async def children_done() -> bool:
        return len(await events(queue_engine)) == 3

    await run_workers([make_worker(queue_engine)], children_done)

    assert await status_of(queue_engine, parent) == TaskStatus.DONE
    assert sorted(e[1] for e in await events(queue_engine)) == ["s1", "s2", "s3"]


async def test_4_28_failed_task_does_not_leave_its_small_tasks(queue_engine: AsyncEngine) -> None:
    """Подзадачи ставятся в транзакции задачи: её провал откатывает и их постановку."""
    parent = await enqueue(queue_engine, spawn, SpawnArgs(labels=["s1"], fail=True))

    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, [parent]))

    assert await _count_tasks(queue_engine) == 1
    assert await events(queue_engine) == []


# --- Перезапуск (4.16) ---


async def test_4_16_unfinished_operations_continue_after_restart(queue_engine: AsyncEngine) -> None:
    """4.16: воркер остановили (SIGTERM) посреди работы — начатая операция доделана,
    остальные выполняет новый воркер, каждую ровно один раз."""
    task_ids = [
        await enqueue(queue_engine, record_event, RecordArgs(label=f"r{n}", sleep=0.1), key="sub:r")
        for n in range(5)
    ]

    async def first_done() -> bool:
        return len(await events(queue_engine)) >= 1

    await run_workers([make_worker(queue_engine)], first_done)
    assert len(await events(queue_engine)) < 5

    await run_workers([make_worker(queue_engine)], lambda: all_finished(queue_engine, task_ids))
    assert [e[1] for e in await events(queue_engine)] == [f"r{n}" for n in range(5)]


# --- Периодические задачи и очистка ---


async def test_periodic_task_runs_once_per_period_with_two_workers(
    queue_engine: AsyncEngine,
) -> None:
    """Периодическая задача не ставится дважды за один период при двух воркерах."""
    every = timedelta(milliseconds=200)
    periodic = [Periodic(record_event, RecordArgs(label="tick"), every)]
    workers = [make_worker(queue_engine, periodic=periodic) for _ in range(2)]

    await run_workers(workers, lambda: _sleep_true(1.1))

    async with queue_engine.connect() as connection:
        slots = await connection.scalar(
            text("SELECT count(*) FROM queue_periodic_slots WHERE name = 'test.record'")
        )
        ticks = await connection.scalar(
            text("SELECT count(*) FROM queue_tasks WHERE name = 'test.record'")
        )
    assert slots is not None
    assert slots >= 4
    assert ticks == slots


async def test_cleanup_removes_old_finished_tasks_only(queue_engine: AsyncEngine) -> None:
    """Очистка удаляет выполненные и отменённые задачи старше 30 дней; проваленные,
    свежие и ждущие остаются; журнал не трогается."""
    labels = ["old_done", "fresh_done", "old_failed", "old_cancelled", "pending"]
    ids = {
        label: await enqueue(queue_engine, record_event, RecordArgs(label=label))
        for label in labels
    }
    finished = {
        "old_done": (TaskStatus.DONE, 31),
        "fresh_done": (TaskStatus.DONE, 29),
        "old_failed": (TaskStatus.FAILED, 40),
        "old_cancelled": (TaskStatus.CANCELLED, 31),
    }
    async with AsyncSession(queue_engine) as session, session.begin():
        for label, (status, days) in finished.items():
            await session.execute(
                update(QueueTask)
                .where(QueueTask.id == ids[label])
                .values(status=status, finished_at=func.now() - timedelta(days=days))
            )
        await session.execute(
            update(QueueTask)
            .where(QueueTask.id == ids["pending"])
            .values(run_at=func.now() + timedelta(days=1))
        )

    worker = make_worker(queue_engine, config=FAST_CONFIG)
    await worker.schedule_periodic()
    await run_workers([worker], lambda: _no_cleanup_pending(queue_engine))

    async with AsyncSession(queue_engine) as session:
        remaining = set(
            await session.scalars(select(QueueTask.id).where(QueueTask.name == "test.record"))
        )
    assert remaining == {ids["fresh_done"], ids["old_failed"], ids["pending"]}


async def _no_cleanup_pending(engine: AsyncEngine) -> bool:
    async with AsyncSession(engine) as session:
        pending = await session.scalar(
            select(func.count()).where(
                QueueTask.name == "queue.cleanup", QueueTask.status != TaskStatus.DONE
            )
        )
    return pending == 0


async def test_unknown_task_name_is_an_ordinary_failure(queue_engine: AsyncEngine) -> None:
    """Задача неизвестного вида (например, после смены версии кода) — обычная ошибка."""
    async with AsyncSession(queue_engine) as session, session.begin():
        session.add(QueueTask(name="test.unknown", args={}))

    await run_workers(
        [make_worker(queue_engine)],
        lambda: _all_tasks_finished(queue_engine),
    )

    async with AsyncSession(queue_engine) as session:
        task_id = await session.scalar(select(QueueTask.id).where(QueueTask.name == "test.unknown"))
        assert task_id is not None
        attempts = await attempts_of(session, task_id)
    assert attempts[0].error == "LookupError: Неизвестный вид задачи test.unknown"


async def _all_tasks_finished(engine: AsyncEngine) -> bool:
    async with AsyncSession(engine) as session:
        ids = list(await session.scalars(select(QueueTask.id)))
    return await all_finished(engine, ids)
