"""Все таблицы магазина. Импорт модулей регистрирует их модели в `Base.metadata`.

Alembic и тесты схемы импортируют этот модуль, чтобы видеть схему целиком.
"""

from remnabay import journal, queue, texts
from remnabay.db import Base
from remnabay.domain import (
    bonus,
    broadcasts,
    clients,
    migration_runs,
    panel_events,
    payments,
    promo,
    referrals,
    reminders,
    subscriptions,
    tariffs,
    team,
    trials,
)

__all__ = [
    "Base",
    "bonus",
    "broadcasts",
    "clients",
    "journal",
    "migration_runs",
    "panel_events",
    "payments",
    "promo",
    "queue",
    "referrals",
    "reminders",
    "subscriptions",
    "tariffs",
    "team",
    "texts",
    "trials",
]
