"""Критерий 1.1 на уровне запуска: каждая роль без параметров .env не стартует."""

import json
import logging

import pytest

from remnabay.cli import EXIT_CONFIG_ERROR, configure_logging, main
from remnabay.crypto import is_valid_key
from tests.conftest import REQUIRED_ENV


@pytest.mark.parametrize("role", ["web", "worker", "migrate"])
@pytest.mark.usefixtures("clean_env")
def test_1_1_role_does_not_start_without_env(role: str, capsys: pytest.CaptureFixture[str]) -> None:
    """1.1: роль завершается с ошибкой и перечисляет недостающие параметры."""
    exit_code = main([role])

    assert exit_code == EXIT_CONFIG_ERROR
    stderr = capsys.readouterr().err
    assert "Магазин не запущен" in stderr
    for name in REQUIRED_ENV:
        assert f"{name}: не задан" in stderr


@pytest.mark.usefixtures("valid_env")
def test_migrate_role_applies_migrations() -> None:
    """`remnabay migrate` с верными параметрами применяет миграции и завершается успешно."""
    assert main(["migrate"]) == 0


@pytest.mark.usefixtures("clean_env")
def test_generate_key_works_before_env_is_filled(capsys: pytest.CaptureFixture[str]) -> None:
    """Ключ шифрования создаётся до заполнения .env и проходит проверку формата (1.1)."""
    assert main(["generate-key"]) == 0

    key = capsys.readouterr().out.strip()
    assert is_valid_key(key)


def test_request_per_line_libraries_log_only_warnings() -> None:
    """Клиент панели не пишет в лог каждый запрос — только предупреждения и ошибки."""
    configure_logging()

    assert logging.getLogger("httpx2").level == logging.WARNING


@pytest.mark.usefixtures("clean_env")
def test_0051_openapi_without_env(capsys: pytest.CaptureFixture[str]) -> None:
    """0051: схема API админки выгружается без .env и базы — её берут при сборке."""
    assert main(["openapi"]) == 0

    schema = json.loads(capsys.readouterr().out)
    assert "/api/admin/auth/me" in schema["paths"]
    assert not [path for path in schema["paths"] if path.startswith("/webhooks")]
