"""Воркер-заглушка и его проверка здоровья по файлу-пульсу (0026)."""

import asyncio
from pathlib import Path

from remnabay.worker.main import is_heartbeat_fresh, run_worker, write_heartbeat


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
