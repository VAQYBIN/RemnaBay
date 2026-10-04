"""Проверки здоровья для Docker (0026). В slim-образе нет curl, поэтому — на Python."""

import http.client

from remnabay.web.app import HEALTH_PATH
from remnabay.worker.main import DEFAULT_HEARTBEAT_PATH, is_heartbeat_fresh

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
WEB_TIMEOUT_SECONDS = 5.0


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
