"""Окружение воркера, которое нужно задачам очереди: отправитель сообщений, клиент панели.

Задача очереди получает только сессию и свои аргументы; внешние клиенты она берёт
отсюда. Окружение задаёт запуск воркера, а тесты — своё, с подставными клиентами.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from remnabay.messaging._sender import Sender
from remnabay.panel import PanelClient


@dataclass(frozen=True)
class Runtime:
    sender: Sender
    panel: PanelClient


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
