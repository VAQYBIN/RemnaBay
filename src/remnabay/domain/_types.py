"""Общие типы колонок модели данных."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import DateTime, Numeric, func
from sqlalchemy.orm import mapped_column

# Деньги — в валюте учёта магазина (решение 0020), с точностью до копейки:
# округление денежных расчётов — до минимальной единицы валюты (04-operator-settings)
type Money = Decimal
MONEY = Numeric(14, 2)

# Время создания строки
type CreatedAt = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=func.now())
]
