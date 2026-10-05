"""Шаг применения платежа: что записать в панель и магазин (блок 3)."""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment


@dataclass(frozen=True)
class Applied:
    """Итог применения: какая подписка получила оплаченное и создана ли она сейчас."""

    subscription_id: int
    created: bool


class PaymentApplier(Protocol):
    """Шаг применения: записывает в панель и магазин то, за что заплачено (блок 3).

    Правила для реализации: перед внешним действием сверить состояние — не создан ли
    уже пользователь, не продлена ли уже подписка (4.15); отрезки срока менять
    только после того, как изменение применено в панели (4.34). Ошибка — повтор
    по правилам очереди; панель недоступна — `PanelUnavailableError` (4.30).
    """

    async def apply(self, session: AsyncSession, payment: Payment) -> Applied: ...


class ApplierNotReadyError(Exception):
    """Шаг применения ещё не подключён."""


class NotReadyApplier:
    """Шаг применения до блока 3: оплат ещё нет — провайдеры появятся вместе с ним."""

    async def apply(self, session: AsyncSession, payment: Payment) -> Applied:
        raise ApplierNotReadyError("Применение покупки и продления появится вместе с блоком 3")
