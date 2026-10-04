"""Все таблицы магазина. Импорт модулей регистрирует их модели в `Base.metadata`.

Alembic и тесты схемы импортируют этот модуль, чтобы видеть схему целиком.
"""

from remnabay import journal, queue
from remnabay.db import Base
from remnabay.domain import bonus, clients, payments, subscriptions, tariffs, team

__all__ = [
    "Base",
    "bonus",
    "clients",
    "journal",
    "payments",
    "queue",
    "subscriptions",
    "tariffs",
    "team",
]
