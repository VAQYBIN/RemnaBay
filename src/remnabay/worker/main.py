"""Роль «воркер»: очередь фоновых задач (0024) и файл-пульс.

Воркер не отвечает по HTTP, поэтому о том, что он жив, говорит файл-пульс:
цикл обновляет его время изменения, проверка здоровья смотрит, свежее ли оно.

Если цикл задач долго не может сделать шаг (например, база недоступна), воркер
сам завершается с ошибкой, и Docker поднимает его заново (0073). Пульс остаётся
на случай, когда процесс завис так, что завершиться сам не может.
"""

import asyncio
import contextlib
import logging
import signal
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.attention import attention
from remnabay.broadcasts import send_step
from remnabay.client_actions import add_granted_days
from remnabay.config import Settings
from remnabay.crypto import SecretBox
from remnabay.db import create_engine
from remnabay.devices import delete_devices
from remnabay.guide import REFRESH_CONFIGS, refresh_configs_task
from remnabay.messaging import TelegramSender, notify_team, send_message
from remnabay.migration import TASKS as MIGRATION_TASKS
from remnabay.panel import PanelClient, PanelUnavailableError
from remnabay.panel_sync import (
    HEALTH_CHECK,
    SYNC_ALL,
    health_check,
    reconcile_subscription,
    sync_page,
)
from remnabay.payments import (
    Providers,
    ShopApplier,
    apply_payment,
    expire_invoice,
    poll_invoice,
    processing_notice,
)
from remnabay.payments.yookassa import YooKassaKind, yookassa_http
from remnabay.promo import add_promo_days
from remnabay.queue import RetryPolicy, TaskRegistry, Worker, WorkerConfig, WorkerStalledError
from remnabay.refunds import change_term
from remnabay.reminders import REMINDERS_TICK, reminders_tick, replan_reminders, send_reminders
from remnabay.shop_settings import (
    RETRY_MAX_ATTEMPTS,
    RETRY_WINDOW,
    claim_interval,
    get_setting,
)
from remnabay.trial import create_trial, trial_processing_notice

DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "remnabay-worker.heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 10.0
# Несколько пропущенных пульсов подряд — воркер считается зависшим
HEARTBEAT_MAX_AGE_SECONDS = 60.0

# Виды задач магазина — общие для воркера и действий команды над проваленными.
# Добавляются блоками, которые их вводят; очистка очереди встроена в сам воркер
TASKS = TaskRegistry(
    (
        send_message,
        reconcile_subscription,
        sync_page,
        health_check,
        apply_payment,
        processing_notice,
        expire_invoice,
        poll_invoice,
        create_trial,
        trial_processing_notice,
        refresh_configs_task,
        delete_devices,
        add_promo_days,
        add_granted_days,
        change_term,
        reminders_tick,
        replan_reminders,
        send_reminders,
        send_step,
        *MIGRATION_TASKS,
    )
)
# Периодические задачи: сверка всех подписок с панелью (4.8), проверка связи с ней (4.13),
# обновление конфигов Subscription Page (12.7), сообщения о сроке подписки (блок 7)
PERIODIC = (SYNC_ALL, HEALTH_CHECK, REFRESH_CONFIGS, REMINDERS_TICK)
# Ошибки «внешний сервис недоступен»: задача ждёт, а не проваливается (4.30)
UNAVAILABLE: tuple[type[Exception], ...] = (PanelUnavailableError,)

# Не чаще одного уведомления «Операция не выполнена» в 10 минут (0049)
OPERATION_FAILED_EVERY = timedelta(minutes=10)
OPERATION_FAILED_MARK = "team.operation_failed.notified_at"

logger = logging.getLogger(__name__)


async def notify_operation_failed(session: AsyncSession, _task_id: int, _name: str) -> None:
    """Операция без своих последствий провала — команда получает уведомление (4.31).

    Не чаще одного в 10 минут, с числом ждущих разбора (0049): при массовом сбое
    уведомлений не столько же, сколько операций.
    """
    now = datetime.now(UTC)
    if not await claim_interval(
        session, OPERATION_FAILED_MARK, now=now, every=OPERATION_FAILED_EVERY
    ):
        return
    waiting = (await attention(session, TASKS)).count
    await notify_team(session, "team.operation_failed", variables={"count": str(waiting)})


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


def run(settings: Settings, config: WorkerConfig | None = None) -> bool:
    """Запускает воркер до SIGTERM или SIGINT.

    `False` — воркер остановился, потеряв работоспособность (0073).
    """

    async def main() -> bool:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signal_number, stop.set)
        worker_config = config or WorkerConfig()
        engine = create_engine(settings, pool_size=worker_config.pool_size)
        sender = TelegramSender(settings.bot_token.get_secret_value())
        panel = PanelClient(str(settings.panel_url), settings.panel_token.get_secret_value())
        yookassa = yookassa_http()
        stalled: BaseException | None = None
        try:
            queue = Worker(
                engine,
                TASKS,
                periodic=PERIODIC,
                policy=retry_policy,
                config=worker_config,
                unavailable=UNAVAILABLE,
                on_failed=notify_operation_failed,
            )
            box = SecretBox(settings.encryption_key.get_secret_value())
            with runtime.use(
                runtime.Runtime(
                    sender=sender,
                    panel=panel,
                    payments=ShopApplier(dev_mode=settings.dev_mode),
                    providers=Providers(box, [YooKassaKind(yookassa)]),
                    dev_mode=settings.dev_mode,
                )
            ):
                try:
                    await run_worker(stop, queue=queue)
                except* WorkerStalledError as group:
                    stalled = group.exceptions[0]
        finally:
            await panel.aclose()
            await yookassa.aclose()
            await sender.close()
            await engine.dispose()
        if stalled is not None:
            logger.critical("Воркер потерял работоспособность и завершается: %s", stalled)
            return False
        return True

    return asyncio.run(main())
