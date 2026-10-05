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
from remnabay.panel_sync._sweep import SYNC_ALL, SyncPageArgs, sync_interval, sync_page

__all__ = [
    "SYNC_ALL",
    "ReconcileArgs",
    "Source",
    "SyncPageArgs",
    "enqueue_reconcile",
    "receive_event",
    "reconcile",
    "reconcile_subscription",
    "sync_interval",
    "sync_page",
    "webhook_received",
]
