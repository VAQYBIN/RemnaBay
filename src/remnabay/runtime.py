"""Окружение воркера, которое нужно задачам очереди: отправитель сообщений, клиент панели,
шаг применения платежа, платёжные провайдеры.

Задача очереди получает только сессию и свои аргументы; внешние клиенты она берёт
отсюда. Окружение задаёт запуск воркера, а тесты — своё, с подставными клиентами.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

# Только для проверки типов: модули задач сами импортируют это окружение, и прямой
# импорт замкнул бы круг (аннотации в Python 3.14 вычисляются лениво)
if TYPE_CHECKING:
    from remnabay.messaging import Sender
    from remnabay.panel import PanelClient
    from remnabay.payments import PaymentApplier, Providers


@dataclass(frozen=True)
class Runtime:
    sender: Sender
    panel: PanelClient
    # Шаг применения платежа: что записать в панель и магазин при покупке и продлении
    payments: PaymentApplier
    # Подключённые провайдеры — для опроса неоплаченных счетов (3.29)
    providers: Providers


_current: ContextVar[Runtime] = ContextVar("remnabay_runtime")


def current() -> Runtime:
    try:
        return _current.get()
    except LookupError as error:
        raise RuntimeError("Окружение воркера не задано: запускайте задачи внутри use()") from error


@contextmanager
def use(runtime: Runtime) -> Generator[Runtime]:
    """Задаёт окружение для кода внутри блока и задач, запущенных из него."""
    token = _current.set(runtime)
    try:
        yield runtime
    finally:
        _current.reset(token)
