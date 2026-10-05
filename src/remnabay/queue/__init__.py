"""Очередь фоновых задач на PostgreSQL (решение 0024).

Остальной код знает только этот интерфейс: описать задачу (`task`) и её исходы
(`on_failed`, `on_cancelled`, `on_resolved`), поставить её в своей транзакции
(`TaskDefinition.enqueue`), запустить воркер (`Worker`), разобрать проваленную
(`retry_failed`, `cancel_failed`, `resolve_failed`) и показать, что требует
внимания (`failed_tasks`, `waiting_panel_count`).

Правила для задач (0024):
- задача сверяет состояние перед внешним действием (4.15): «оживший» после
  зависания воркер может доделать устаревшую попытку;
- большая работа делится на много небольших задач (4.28).
"""

from remnabay.queue._core import (
    Ending,
    FailedTask,
    RejectedError,
    RetryPolicy,
    TaskContext,
    TaskDefinition,
    TaskNotCancellableError,
    TaskNotFailedError,
    TaskRegistry,
    attempts_of,
    cancel_failed,
    failed_tasks,
    resolve_failed,
    retry_failed,
    task,
    task_status,
    task_subject,
    unfinished_keys,
    waiting_behind,
    waiting_panel_count,
)
from remnabay.queue._models import AttemptResult, TaskStatus
from remnabay.queue._worker import (
    FailedFallback,
    IntervalSource,
    Periodic,
    PolicySource,
    TaskTimeoutError,
    Worker,
    WorkerConfig,
)

__all__ = [
    "AttemptResult",
    "Ending",
    "FailedFallback",
    "FailedTask",
    "IntervalSource",
    "Periodic",
    "PolicySource",
    "RejectedError",
    "RetryPolicy",
    "TaskContext",
    "TaskDefinition",
    "TaskNotCancellableError",
    "TaskNotFailedError",
    "TaskRegistry",
    "TaskStatus",
    "TaskTimeoutError",
    "Worker",
    "WorkerConfig",
    "attempts_of",
    "cancel_failed",
    "failed_tasks",
    "resolve_failed",
    "retry_failed",
    "task",
    "task_status",
    "task_subject",
    "unfinished_keys",
    "waiting_behind",
    "waiting_panel_count",
]
