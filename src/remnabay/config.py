"""Настройки магазина из `.env` и окружения (критерий 1.1).

В `.env` — только секреты и инфраструктура, без которых магазин не запустится
(`docs/04-operator-settings.md`). Всё остальное настраивается в админке.
"""

from pathlib import Path
from typing import Annotated

from pydantic import (
    AfterValidator,
    HttpUrl,
    PositiveInt,
    PostgresDsn,
    SecretStr,
    ValidationError,
)
from pydantic_core import ErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from remnabay.crypto import is_valid_key

DEFAULT_ENV_FILE = Path(".env")

# Драйвер psycopg 3 нужен очереди задач (решение 0024, эксперимент с очередью)
_PSYCOPG_SCHEME = "postgresql+psycopg"
_ALLOWED_DATABASE_SCHEMES = frozenset({"postgres", "postgresql", _PSYCOPG_SCHEME})


def _require_https(url: HttpUrl) -> HttpUrl:
    if url.scheme != "https":
        raise PydanticCustomError("https_required", "адрес должен начинаться с https://")
    return url


def _require_psycopg(dsn: PostgresDsn) -> PostgresDsn:
    if dsn.scheme not in _ALLOWED_DATABASE_SCHEMES:
        raise PydanticCustomError(
            "database_driver",
            "поддерживается только postgresql:// или postgresql+psycopg://",
        )
    return dsn


def _require_encryption_key(key: SecretStr) -> SecretStr:
    if not is_valid_key(key.get_secret_value()):
        raise PydanticCustomError(
            "encryption_key", "неверный формат ключа — создайте ключ командой remnabay generate-key"
        )
    return key


type HttpsUrl = Annotated[HttpUrl, AfterValidator(_require_https)]
type EncryptionKey = Annotated[SecretStr, AfterValidator(_require_encryption_key)]
type PsycopgDsn = Annotated[PostgresDsn, AfterValidator(_require_psycopg)]


class Settings(BaseSettings):
    """Параметры `.env`: все обязательные, кроме режима разработки.

    Пустое значение считается незаданным.
    """

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    bot_token: SecretStr
    panel_url: HttpUrl
    panel_token: SecretStr
    database_url: PsycopgDsn
    owner_telegram_id: PositiveInt
    public_url: HttpsUrl
    encryption_key: EncryptionKey
    # Режим разработки (0049, 0051): документация API, бот опросом, cookie без HTTPS.
    # Оператор его не задаёт
    dev_mode: bool = False

    @property
    def sqlalchemy_database_url(self) -> str:
        """Адрес базы для SQLAlchemy — всегда с драйвером psycopg 3."""
        url = str(self.database_url)
        scheme, rest = url.split("://", 1)
        if scheme == _PSYCOPG_SCHEME:
            return url
        return f"{_PSYCOPG_SCHEME}://{rest}"


class ConfigError(Exception):
    """Параметры `.env` не заданы или неверны: магазин не запускается (1.1)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(self.render())

    def render(self) -> str:
        lines = ["Магазин не запущен: проверьте параметры в .env"]
        lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


_MESSAGES_BY_ERROR_TYPE = {
    "missing": "не задан",
    "int_parsing": "ожидается целое число",
    "int_from_float": "ожидается целое число",
    "greater_than": "должен быть положительным числом",
    "url_parsing": "ожидается адрес, например https://example.com",
    "url_scheme": "неподдерживаемая схема адреса",
    "bool_parsing": "ожидается true или false",
}


def _describe(error: ErrorDetails) -> str:
    field = str(error["loc"][0]) if error["loc"] else "?"
    message = _MESSAGES_BY_ERROR_TYPE.get(error["type"], error["msg"])
    return f"{field.upper()}: {message}"


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Читает настройки; при любой проблеме бросает `ConfigError` со списком параметров."""
    try:
        # pyright строит __init__ модели по полям и не видит, что их заполняют
        # источники pydantic-settings (окружение, .env)
        return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        raise ConfigError([_describe(error) for error in exc.errors()]) from exc
