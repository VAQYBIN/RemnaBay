"""Проверка здоровья веба (0026): 200 при живой базе, 503 при недоступной."""

import logging
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from remnabay.config import load_settings
from remnabay.healthcheck import HEALTH_PATH
from remnabay.web.app import Watchdog, create_app


@pytest.mark.usefixtures("valid_env")
def test_health_ok_when_database_available() -> None:
    """База отвечает — 200 и status ok."""
    with TestClient(create_app(load_settings(env_file=None))) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.usefixtures("valid_env")
def test_health_unavailable_when_database_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """База недоступна — 503, status unavailable и одна строка в логе без трассировки."""
    # Порт 1 на localhost закрыт: соединение сразу отклоняется
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")

    with TestClient(create_app(load_settings(env_file=None))) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("Проверка здоровья: база недоступна (")
    assert warnings[0].exc_info is None


def _wait_for(condition: Callable[[], bool], limit_seconds: float = 5) -> bool:
    deadline = time.monotonic() + limit_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


FAST_WATCH = {"interval": timedelta(milliseconds=20), "lost_after": timedelta(milliseconds=200)}


@pytest.mark.usefixtures("valid_env")
def test_0073_web_stops_when_database_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """0073: база дольше предела не отвечает — веб сообщает, что потерял работоспособность."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    lost: list[bool] = []
    watchdog = Watchdog(on_lost=lambda: lost.append(True), **FAST_WATCH)

    with TestClient(create_app(load_settings(env_file=None), watchdog=watchdog)):
        assert _wait_for(lambda: bool(lost))

    assert lost == [True]
    assert any(
        r.levelno == logging.CRITICAL and "Веб потерял работоспособность" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.usefixtures("valid_env")
def test_0073_web_keeps_running_with_database() -> None:
    """0073: при живой базе самоконтроль веба не срабатывает."""
    lost: list[bool] = []
    watchdog = Watchdog(on_lost=lambda: lost.append(True), **FAST_WATCH)

    with TestClient(create_app(load_settings(env_file=None), watchdog=watchdog)):
        time.sleep(FAST_WATCH["lost_after"].total_seconds() * 3)

    assert lost == []
