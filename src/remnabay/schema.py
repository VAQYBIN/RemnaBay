"""Все таблицы магазина. Импорт модулей регистрирует их модели в `Base.metadata`.

Alembic и тесты схемы импортируют этот модуль, чтобы видеть схему целиком.
"""

from remnabay import journal
from remnabay.db import Base

__all__ = ["Base", "journal"]
