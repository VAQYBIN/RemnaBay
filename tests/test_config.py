"""Критерий 1.1: без обязательного параметра магазин не запускается и называет его."""

from pathlib import Path

import pytest

from remnabay.config import ConfigError, load_settings
from tests.conftest import REQUIRED_ENV


@pytest.mark.usefixtures("valid_env")
def test_1_1_full_set_loads() -> None:
    """1.1: полный набор параметров загружается."""
    settings = load_settings(env_file=None)

    assert settings.owner_telegram_id == 100500
    assert str(settings.public_url) == "https://shop.example.com/"
    assert settings.bot_token.get_secret_value() == REQUIRED_ENV["BOT_TOKEN"]


@pytest.mark.parametrize("name", sorted(REQUIRED_ENV))
@pytest.mark.usefixtures("valid_env")
def test_1_1_missing_parameter_is_named(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """1.1: отсутствие каждого обязательного параметра называется в ошибке."""
    monkeypatch.delenv(name)

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    assert exc_info.value.problems == [f"{name}: не задан"]


@pytest.mark.usefixtures("valid_env")
def test_1_1_empty_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """1.1: пустое значение равносильно незаданному параметру."""
    monkeypatch.setenv("BOT_TOKEN", "")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    assert exc_info.value.problems == ["BOT_TOKEN: не задан"]


@pytest.mark.usefixtures("clean_env")
def test_1_1_all_missing_parameters_are_listed() -> None:
    """1.1: при пустом окружении перечисляются все обязательные параметры."""
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    assert sorted(exc_info.value.problems) == sorted(f"{name}: не задан" for name in REQUIRED_ENV)
    assert "BOT_TOKEN" in str(exc_info.value)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("OWNER_TELEGRAM_ID", "abc", "ожидается целое число"),
        ("OWNER_TELEGRAM_ID", "0", "должен быть положительным числом"),
        ("PANEL_URL", "not a url", "ожидается адрес, например https://example.com"),
        ("PUBLIC_URL", "http://shop.example.com", "адрес должен начинаться с https://"),
        (
            "DATABASE_URL",
            "postgresql+asyncpg://u:p@localhost/db",
            "поддерживается только postgresql:// или postgresql+psycopg://",
        ),
        ("DATABASE_URL", "mysql://u:p@localhost/db", "неподдерживаемая схема адреса"),
    ],
)
@pytest.mark.usefixtures("valid_env")
def test_1_1_invalid_value_is_named(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    """1.1: неверное значение параметра называется в ошибке с причиной."""
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    assert exc_info.value.problems == [f"{name}: {message}"]


@pytest.mark.usefixtures("clean_env")
def test_1_1_reads_env_file(tmp_path: Path) -> None:
    """1.1: параметры читаются из файла `.env`."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in REQUIRED_ENV.items()), encoding="utf-8"
    )

    settings = load_settings(env_file=env_file)

    assert settings.panel_token.get_secret_value() == REQUIRED_ENV["PANEL_TOKEN"]


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("postgresql://u:p@db:5432/shop", "postgresql+psycopg://u:p@db:5432/shop"),
        ("postgres://u:p@db:5432/shop", "postgresql+psycopg://u:p@db:5432/shop"),
        ("postgresql+psycopg://u:p@db:5432/shop", "postgresql+psycopg://u:p@db:5432/shop"),
    ],
)
@pytest.mark.usefixtures("valid_env")
def test_database_url_uses_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch, database_url: str, expected: str
) -> None:
    """Адрес базы для SQLAlchemy всегда с драйвером psycopg 3 (0024)."""
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert load_settings(env_file=None).sqlalchemy_database_url == expected


@pytest.mark.usefixtures("valid_env")
def test_1_1_encryption_key_must_have_valid_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """1.1: ключ шифрования неверного формата — магазин не запускается и подсказывает,
    как создать ключ."""
    monkeypatch.setenv("ENCRYPTION_KEY", "my-secret-password")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    [problem] = exc_info.value.problems
    assert problem.startswith("ENCRYPTION_KEY: неверный формат ключа")
    assert "remnabay generate-key" in problem


@pytest.mark.usefixtures("valid_env")
def test_0051_dev_mode_is_off_by_default() -> None:
    """0051: без DEV_MODE магазин работает в рабочем режиме."""
    assert load_settings(env_file=None).dev_mode is False


@pytest.mark.usefixtures("valid_env")
def test_0051_dev_mode_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")

    assert load_settings(env_file=None).dev_mode is True


@pytest.mark.usefixtures("valid_env")
def test_0051_dev_mode_invalid_value_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "maybe")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env_file=None)

    assert exc_info.value.problems == ["DEV_MODE: ожидается true или false"]
