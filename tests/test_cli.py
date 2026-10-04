"""Критерий 1.1 на уровне запуска: каждая роль без параметров .env не стартует."""

import pytest

from remnabay.cli import EXIT_CONFIG_ERROR, main
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
