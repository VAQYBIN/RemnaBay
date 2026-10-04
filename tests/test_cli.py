"""Критерий 1.1 на уровне запуска: каждая роль без параметров .env не стартует."""

import pytest

from remnabay.cli import EXIT_CONFIG_ERROR, main
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
