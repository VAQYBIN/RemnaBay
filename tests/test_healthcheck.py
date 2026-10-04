"""Команда проверки здоровья веба для Docker (0026)."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from remnabay.healthcheck import check_web
from remnabay.web.app import HEALTH_PATH


def _serve(status_code: int) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status_code if self.path == HEALTH_PATH else 404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def healthy_web() -> Iterator[int]:
    yield from _serve(200)


@pytest.fixture
def unhealthy_web() -> Iterator[int]:
    yield from _serve(503)


def test_check_web_healthy(healthy_web: int) -> None:
    """/health отвечает 200 — веб здоров."""
    assert check_web(port=healthy_web)


def test_check_web_unhealthy(unhealthy_web: int) -> None:
    """/health отвечает 503 — веб нездоров."""
    assert not check_web(port=unhealthy_web)


def test_check_web_not_listening() -> None:
    """Сервер не слушает порт — веб нездоров, без исключения."""
    assert not check_web(port=1)
