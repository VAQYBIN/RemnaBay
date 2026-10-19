"""Роль «воркер»: очередь задач и проверка здоровья по файлу-пульсу (0026)."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from remnabay.config import load_settings
from remnabay.panel import PanelUnavailableError
from remnabay.queue import TaskStatus, WorkerConfig, WorkerStalledError, attempts_of
from remnabay.worker.main import (
    UNAVAILABLE,
    is_heartbeat_fresh,
    run,
    run_worker,
    write_heartbeat,
)
from tests.queue_support import (
    RecordArgs,
    enqueue,
    make_worker,
    record_event,
    status_of,
    wait_until,
)


async def test_worker_writes_heartbeat_and_removes_it_on_stop(tmp_path: Path) -> None:
    """Работающий воркер обновляет пульс; при остановке файл убирается."""
    heartbeat = tmp_path / "heartbeat"
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop, heartbeat, interval=0.01))

    # Первый пульс пишется сразу при старте цикла
    await asyncio.sleep(0.05)
    assert is_heartbeat_fresh(heartbeat)

    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert not heartbeat.exists()


def test_heartbeat_fresh_and_stale(tmp_path: Path) -> None:
    """Пульс свежий в пределах допустимого возраста и устаревший за ним."""
    heartbeat = tmp_path / "heartbeat"
    write_heartbeat(heartbeat)
    modified_at = heartbeat.stat().st_mtime

    assert is_heartbeat_fresh(heartbeat, max_age=60, now=modified_at + 59)
    assert not is_heartbeat_fresh(heartbeat, max_age=60, now=modified_at + 61)


def test_missing_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    """Нет файла-пульса — воркер не считается живым."""
    assert not is_heartbeat_fresh(tmp_path / "missing")


async def test_worker_runs_queue_and_finishes_started_task_on_stop(
    tmp_path: Path, queue_engine: AsyncEngine
) -> None:
    """Воркер выполняет задачи очереди; остановка (SIGTERM) дожидается начатой задачи."""
    task_id = await enqueue(queue_engine, record_event, RecordArgs(label="w", sleep=0.3))
    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_worker(stop, tmp_path / "heartbeat", interval=0.01, queue=make_worker(queue_engine))
    )

    await wait_until(lambda: _attempt_started(queue_engine, task_id), limit_seconds=5)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert await status_of(queue_engine, task_id) == TaskStatus.DONE


async def _attempt_started(engine: AsyncEngine, task_id: int) -> bool:
    async with AsyncSession(engine) as session:
        return bool(await attempts_of(session, task_id))


def test_4_30_panel_unavailability_makes_task_wait() -> None:
    """4.30: воркер считает недоступность панели ожиданием, а не провалом."""
    assert PanelUnavailableError in UNAVAILABLE


# Быстрые пределы для проверок самоконтроля: таймаут задачи меньше предела простоя
STALL_CONFIG = WorkerConfig(
    concurrency=2,
    poll_interval=timedelta(milliseconds=20),
    task_timeout=timedelta(milliseconds=100),
    idle_in_transaction_timeout=timedelta(seconds=1),
    stall_limit=timedelta(milliseconds=300),
)
# Порт 1 на localhost закрыт: соединение с «базой» сразу отклоняется
UNREACHABLE_DATABASE = "postgresql+psycopg://u:p@127.0.0.1:1/none"


async def test_0073_worker_stops_when_database_unreachable(tmp_path: Path) -> None:
    """0073: цикл задач не может сделать шаг (база недоступна) — воркер останавливается
    с ошибкой, чтобы Docker поднял его заново."""
    engine = create_async_engine(UNREACHABLE_DATABASE)
    try:
        queue = make_worker(engine, config=STALL_CONFIG)
        with pytest.raises(ExceptionGroup) as raised:
            await asyncio.wait_for(
                run_worker(asyncio.Event(), tmp_path / "heartbeat", interval=0.01, queue=queue),
                timeout=5,
            )
    finally:
        await engine.dispose()

    assert raised.group_contains(WorkerStalledError)
    assert not (tmp_path / "heartbeat").exists()


async def test_0073_healthy_worker_keeps_running(tmp_path: Path, queue_engine: AsyncEngine) -> None:
    """0073: при живой базе воркер работает дольше предела простоя и не останавливается."""
    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_worker(
            stop,
            tmp_path / "heartbeat",
            interval=0.01,
            queue=make_worker(queue_engine, config=STALL_CONFIG),
        )
    )

    await asyncio.sleep(STALL_CONFIG.stall_limit.total_seconds() * 3)
    assert not runner.done()

    stop.set()
    await asyncio.wait_for(runner, timeout=5)


@pytest.mark.usefixtures("valid_env")
def test_0073_worker_role_reports_loss_of_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """0073: роль «воркер» с недоступной базой завершается с признаком потери работоспособности."""
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE)

    assert run(load_settings(env_file=None), STALL_CONFIG) is False


def test_0073_stall_limit_longer_than_task_timeout() -> None:
    """0073: предел простоя цикла не может быть меньше таймаута задачи — иначе здоровая
    долгая задача выглядела бы зависшим циклом."""
    with pytest.raises(ValueError, match="Предел простоя"):
        WorkerConfig(task_timeout=timedelta(seconds=30), stall_limit=timedelta(seconds=30))
