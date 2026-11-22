"""Знание магазина о доступе совпадает с панелью (сценарий «Реакция на изменения в панели»).

Два канала: вебхуки панели (`remnabay.web`) и периодическая сверка всех подписок.
Оба ставят одну и ту же операцию — сверку подписки.
"""

from remnabay.panel_sync._events import receive_event, webhook_received
from remnabay.panel_sync._outage import (
    HEALTH_CHECK,
    PanelOutage,
    current_outage,
    health_check,
    panel_available,
)
from remnabay.panel_sync._reconcile import (
    STRATEGIES,
    ReconcileArgs,
    Source,
    accept_access,
    accept_user,
    enqueue_reconcile,
    reconcile,
    reconcile_subscription,
    term_end,
)
from remnabay.panel_sync._sweep import SYNC_ALL, SyncPageArgs, sync_interval, sync_page

__all__ = [
    "HEALTH_CHECK",
    "STRATEGIES",
    "SYNC_ALL",
    "PanelOutage",
    "ReconcileArgs",
    "Source",
    "SyncPageArgs",
    "accept_access",
    "accept_user",
    "current_outage",
    "enqueue_reconcile",
    "health_check",
    "panel_available",
    "receive_event",
    "reconcile",
    "reconcile_subscription",
    "sync_interval",
    "sync_page",
    "term_end",
    "webhook_received",
]
