"""Знание магазина о доступе совпадает с панелью (сценарий «Реакция на изменения в панели»).

Два канала: вебхуки панели (`remnabay.web`) и периодическая сверка всех подписок.
Оба ставят одну и ту же операцию — сверку подписки.
"""

from remnabay.panel_sync._events import receive_event, webhook_received
from remnabay.panel_sync._reconcile import (
    ReconcileArgs,
    Source,
    enqueue_reconcile,
    reconcile,
    reconcile_subscription,
)

__all__ = [
    "ReconcileArgs",
    "Source",
    "enqueue_reconcile",
    "receive_event",
    "reconcile",
    "reconcile_subscription",
    "webhook_received",
]
