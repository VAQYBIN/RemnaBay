"""Цикл воркера. Пока без задач: очередь появится на этапе 2.

Воркер не отвечает по HTTP, поэтому о том, что он жив, говорит файл-пульс:
цикл обновляет его время изменения, проверка здоровья смотрит, свежее ли оно.
"""

import asyncio
import contextlib
import logging
import signal
import tempfile
import time
from pathlib import Path

DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "remnabay-worker.heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 10.0
# Несколько пропущенных пульсов подряд — воркер считается зависшим
HEARTBEAT_MAX_AGE_SECONDS = 60.0

logger = logging.getLogger(__name__)


# Операции с маленьким локальным файлом не блокируют цикл заметно
def write_heartbeat(path: Path) -> None:
    path.touch()


def remove_heartbeat(path: Path) -> None:
    path.unlink(missing_ok=True)


def is_heartbeat_fresh(
    path: Path, max_age: float = HEARTBEAT_MAX_AGE_SECONDS, now: float | None = None
) -> bool:
    try:
        modified_at = path.stat().st_mtime
    except FileNotFoundError:
        return False
    current = time.time() if now is None else now
    return current - modified_at <= max_age


async def run_worker(
    stop: asyncio.Event,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Работает, пока не выставлен `stop`; при выходе убирает файл-пульс."""
    logger.info("Воркер запущен")
    try:
        while not stop.is_set():
            write_heartbeat(heartbeat_path)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        remove_heartbeat(heartbeat_path)
        logger.info("Воркер остановлен")


def run() -> None:
    """Запускает воркер до SIGTERM или SIGINT."""

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signal_number, stop.set)
        await run_worker(stop)

    asyncio.run(main())
