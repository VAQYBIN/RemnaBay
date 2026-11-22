"""Режим работы веба (0049, 0051): документация API только при разработке."""

import pytest
from fastapi.testclient import TestClient

from remnabay.config import load_settings
from remnabay.web.app import create_app

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


@pytest.mark.parametrize("path", DOC_PATHS)
@pytest.mark.usefixtures("valid_env")
def test_0049_api_docs_closed_in_production(path: str) -> None:
    """0049: в рабочем режиме документация API закрыта."""
    with TestClient(create_app(load_settings(env_file=None))) as client:
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", DOC_PATHS)
@pytest.mark.usefixtures("valid_env")
def test_0049_api_docs_open_in_dev_mode(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """0049: при разработке документация API доступна."""
    monkeypatch.setenv("DEV_MODE", "true")

    with TestClient(create_app(load_settings(env_file=None))) as client:
        assert client.get(path).status_code == 200
