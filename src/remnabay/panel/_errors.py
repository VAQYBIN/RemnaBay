"""Ошибки клиента панели.

Недоступность панели (4.30) отделена от остальных ошибок: соединение не
устанавливается, таймаут, ошибки шлюза 502, 503, 504. Такая операция «ждёт
панель» и продолжается сама. Любая другая ошибка — обычный провал с повторами.
"""


class PanelError(Exception):
    """Любая ошибка обращения к панели."""


class PanelUnavailableError(PanelError):
    """До панели не достучаться (4.30)."""


class PanelRequestError(PanelError):
    """Панель ответила ошибкой: неверный запрос, нет прав, внутренняя ошибка."""

    def __init__(self, status_code: int, error_code: str | None, message: str) -> None:
        super().__init__(f"Панель ответила {status_code} {error_code or ''}: {message}".strip())
        self.status_code = status_code
        self.error_code = error_code


class PanelResponseError(PanelError):
    """Ответ панели не совпал с ожидаемой моделью."""
