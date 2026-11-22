"""Проверки здоровья для Docker (0026). В slim-образе нет curl, поэтому — на Python.

Docker запускает проверку каждые 15 секунд отдельным процессом с таймаутом 10 секунд,
поэтому модуль лёгкий: только стандартная библиотека, без импорта приложения. Иначе
одна загрузка aiogram и FastAPI на слабом сервере занимает больше таймаута, и здоровый
процесс выглядит unhealthy. Путь проверки и файл-пульс веб и воркер берут отсюда.
"""

import http.client
import tempfile
import time
from pathlib import Path

HEALTH_PATH = "/health"
WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
WEB_TIMEOUT_SECONDS = 5.0

# Воркер не отвечает по HTTP, поэтому о том, что он жив, говорит файл-пульс:
# цикл обновляет его время изменения, проверка смотрит, свежее ли оно
DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "remnabay-worker.heartbeat"
# Несколько пропущенных пульсов подряд — воркер считается зависшим
HEARTBEAT_MAX_AGE_SECONDS = 60.0


def check_web(host: str = WEB_HOST, port: int = WEB_PORT) -> bool:
    connection = http.client.HTTPConnection(host, port, timeout=WEB_TIMEOUT_SECONDS)
    try:
        connection.request("GET", HEALTH_PATH)
        return connection.getresponse().status == http.client.OK
    except OSError:
        return False
    finally:
        connection.close()


def check_worker() -> bool:
    return is_heartbeat_fresh(DEFAULT_HEARTBEAT_PATH)


# Операции с маленьким локальным файлом не блокируют цикл воркера заметно
def write_heartbeat(path: Path) -> None:
    path.touch()


def remove_heartbeat(path: Path) -> None:
    path.unlink(missing_ok=True)


def is_heartbeat_fresh(
    path: Path, max_age: float = HEARTBEAT_MAX_AGE_SECONDS, now: float | None = None
) -> bool:
    try:
        modified_at = path.stat().st_mtime
    except FileNotFoundError:
        return False
    current = time.time() if now is None else now
    return current - modified_at <= max_age
