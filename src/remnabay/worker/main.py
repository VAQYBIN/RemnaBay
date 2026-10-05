"""Роль «воркер»: очередь фоновых задач (0024) и файл-пульс.

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

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.config import Settings
from remnabay.db import create_engine
from remnabay.messaging import TelegramSender, notify_team, send_message
from remnabay.panel import PanelClient, PanelUnavailableError
from remnabay.panel_sync import SYNC_ALL, reconcile_subscription, sync_page
from remnabay.queue import RetryPolicy, TaskRegistry, Worker, WorkerConfig
from remnabay.shop_settings import RETRY_MAX_ATTEMPTS, RETRY_WINDOW, get_setting

DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "remnabay-worker.heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 10.0
# Несколько пропущенных пульсов подряд — воркер считается зависшим
HEARTBEAT_MAX_AGE_SECONDS = 60.0

# Виды задач магазина — общие для воркера и действий команды над проваленными.
# Добавляются блоками, которые их вводят; очистка очереди встроена в сам воркер
TASKS = TaskRegistry((send_message, reconcile_subscription, sync_page))
# Периодические задачи: сверка всех подписок с панелью (4.8)
PERIODIC = (SYNC_ALL,)
# Ошибки «внешний сервис недоступен»: задача ждёт, а не проваливается (4.30)
UNAVAILABLE: tuple[type[Exception], ...] = (PanelUnavailableError,)

logger = logging.getLogger(__name__)


async def notify_operation_failed(session: AsyncSession, _task_id: int, _name: str) -> None:
    """Операция без своих последствий провала — команда получает уведомление (4.31)."""
    await notify_team(session, "team.operation_failed")


async def retry_policy(session: AsyncSession) -> RetryPolicy:
    """Политика повторов: лимит попыток и окно — из настроек оператора (4.14)."""
    return RetryPolicy(
        max_attempts=await get_setting(session, RETRY_MAX_ATTEMPTS),
        max_age=await get_setting(session, RETRY_WINDOW),
    )


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


async def _beat(stop: asyncio.Event, heartbeat_path: Path, interval: float) -> None:
    while not stop.is_set():
        write_heartbeat(heartbeat_path)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run_worker(
    stop: asyncio.Event,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    queue: Worker | None = None,
) -> None:
    """Работает, пока не выставлен `stop`; при выходе убирает файл-пульс.

    Начатые задачи очереди доделываются до выхода.
    """
    logger.info("Воркер запущен")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_beat(stop, heartbeat_path, interval))
            if queue is not None:
                group.create_task(queue.run(stop))
    finally:
        remove_heartbeat(heartbeat_path)
        logger.info("Воркер остановлен")


def run(settings: Settings) -> None:
    """Запускает воркер до SIGTERM или SIGINT."""

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signal_number, stop.set)
        config = WorkerConfig()
        engine = create_engine(settings, pool_size=config.pool_size)
        sender = TelegramSender(settings.bot_token.get_secret_value())
        panel = PanelClient(str(settings.panel_url), settings.panel_token.get_secret_value())
        try:
            queue = Worker(
                engine,
                TASKS,
                periodic=PERIODIC,
                policy=retry_policy,
                config=config,
                unavailable=UNAVAILABLE,
                on_failed=notify_operation_failed,
            )
            with runtime.use(runtime.Runtime(sender=sender, panel=panel)):
                await run_worker(stop, queue=queue)
        finally:
            await panel.aclose()
            await sender.close()
            await engine.dispose()

    asyncio.run(main())
