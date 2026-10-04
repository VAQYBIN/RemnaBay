"""Общие фикстуры тестов."""

import os
from pathlib import Path

import pytest

REQUIRED_ENV = {
    "BOT_TOKEN": "123456:test-bot-token",
    "PANEL_URL": "https://panel.example.com",
    "PANEL_TOKEN": "test-panel-token",
    "DATABASE_URL": os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://remnabay:remnabay@localhost:5432/remnabay_test",
    ),
    "OWNER_TELEGRAM_ID": "100500",
    "PUBLIC_URL": "https://shop.example.com",
    "ENCRYPTION_KEY": "test-encryption-key",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Окружение без параметров магазина и без чужого `.env` в рабочей папке."""
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def valid_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Полный набор обязательных параметров."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(REQUIRED_ENV)
