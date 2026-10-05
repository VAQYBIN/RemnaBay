"""Все таблицы магазина. Импорт модулей регистрирует их модели в `Base.metadata`.

Alembic и тесты схемы импортируют этот модуль, чтобы видеть схему целиком.
"""

from remnabay import brand, journal, panel_sync, queue, shop_settings, texts
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
    "brand",
    "broadcasts",
    "clients",
    "journal",
    "migration_runs",
    "panel_events",
    "panel_sync",
    "payments",
    "promo",
    "queue",
    "referrals",
    "reminders",
    "shop_settings",
    "subscriptions",
    "tariffs",
    "team",
    "texts",
    "trials",
]
