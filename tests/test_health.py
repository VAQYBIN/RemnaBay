"""Проверка здоровья веба (0026): 200 при живой базе, 503 при недоступной."""

import logging

import pytest
from fastapi.testclient import TestClient

from remnabay.config import load_settings
from remnabay.web.app import HEALTH_PATH, create_app


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
