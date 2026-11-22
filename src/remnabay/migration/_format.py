"""Промежуточный формат миграции, версия 1 (9.8, migration-format.md).

Публичный контракт между адаптерами чужих ботов и импортом. Импорт принимает только
известные версии; неизвестные ключи пропускает, чтобы новое необязательное поле не
ломало старый импорт.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

SUPPORTED_VERSIONS = (1,)


class FormatError(Exception):
    """Файл не в промежуточном формате или в неизвестной версии."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Source(_Model):
    name: str = Field(min_length=1, max_length=128)
    version: str | None = Field(default=None, max_length=64)


class PanelUserRef(_Model):
    """Пользователь панели: хватает любого одного идентификатора (решение 0043)."""

    id: int | None = None
    short_uuid: str | None = Field(default=None, min_length=1, max_length=64)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    # Тариф старого бота — подсказка для сопоставления (9.15)
    tariff_hint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _identified(self) -> Self:
        if self.id is None and self.short_uuid is None and self.username is None:
            raise ValueError("нужен id, short_uuid или username пользователя панели")
        return self

    def label(self) -> str:
        """Как показать ссылку в отчёте."""
        return self.username or self.short_uuid or f"id {self.id}"


class FileClient(_Model):
    # Проверка, что ID больше нуля, — в пробном прогоне: такая запись попадает
    # в «не распознано», а не ломает весь файл
    telegram_id: int
    username: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=256)
    language: str | None = Field(default=None, max_length=16)
    first_seen_at: AwareDatetime | None = None
    trial_used: bool = False
    # Нет поля — файл об оплатах ничего не говорит (решение 0070)
    paid_in_old_bot: bool | None = None
    panel_users: list[PanelUserRef] = Field(default_factory=list[PanelUserRef])
    referral_codes: list[str] = Field(default_factory=list[str])
    invited_by: int | None = None


class MigrationFile(_Model):
    format_version: Literal[1]
    source: Source
    clients: list[FileClient]


class _Header(_Model):
    format_version: int


def _first_error(error: ValidationError) -> str:
    detail = error.errors()[0]
    where = ".".join(str(part) for part in detail["loc"]) or "файл"
    return f"{where}: {detail['msg']}"


def parse_file(raw: bytes) -> MigrationFile:
    """Файл миграции или `FormatError` с причиной, понятной оператору."""
    try:
        header = _Header.model_validate_json(raw)
    except ValidationError as error:
        raise FormatError(
            f"Это не файл промежуточного формата миграции ({_first_error(error)})"
        ) from error
    if header.format_version not in SUPPORTED_VERSIONS:
        supported = ", ".join(str(version) for version in SUPPORTED_VERSIONS)
        raise FormatError(
            f"Версия формата {header.format_version} не поддерживается. "
            f"Поддерживаемые версии: {supported}"
        )
    try:
        return MigrationFile.model_validate_json(raw)
    except ValidationError as error:
        raise FormatError(f"Файл не прошёл проверку: {_first_error(error)}") from error
