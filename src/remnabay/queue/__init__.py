"""Очередь фоновых задач на PostgreSQL (решение 0024).

Остальной код знает только этот интерфейс: описать задачу (`task`), поставить её
в своей транзакции (`TaskDefinition.enqueue`), запустить воркер (`Worker`),
повторить или отменить проваленную (`retry_failed`, `cancel_failed`).

Правила для задач (0024):
- задача сверяет состояние перед внешним действием (4.15): «оживший» после
  зависания воркер может доделать устаревшую попытку;
- большая работа делится на много небольших задач (4.28).
"""

from remnabay.queue._core import (
    RetryPolicy,
    TaskContext,
    TaskDefinition,
    TaskNotFailedError,
    attempts_of,
    cancel_failed,
    retry_failed,
    task,
    task_status,
    task_subject,
    waiting_behind,
)
from remnabay.queue._models import AttemptResult, TaskStatus
from remnabay.queue._worker import Periodic, TaskTimeoutError, Worker, WorkerConfig

__all__ = [
    "AttemptResult",
    "Periodic",
    "RetryPolicy",
    "TaskContext",
    "TaskDefinition",
    "TaskNotFailedError",
    "TaskStatus",
    "TaskTimeoutError",
    "Worker",
    "WorkerConfig",
    "attempts_of",
    "cancel_failed",
    "retry_failed",
    "task",
    "task_status",
    "task_subject",
    "waiting_behind",
]
