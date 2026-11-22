"""Конфиг Subscription Page — только то, из чего собирается инструкция (12.1–12.5).

Модели написаны по конфигу Subscription Page панели 3.4.4 (версия конфига «1»):
платформы, у платформы — приложения, у приложения — шаги с кнопками. Тексты —
словари «язык → текст». Неизвестные поля игнорируются; SVG-иконки и брендинг
веб-страницы в бот не переносятся (12.5). Неизвестный вид кнопки пропускается.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

# Тексты на нескольких языках: «ru» → «Установка»
type Localized = dict[str, str]


class _ConfigModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class ButtonType(StrEnum):
    # Обычная ссылка (12.4)
    EXTERNAL = "external"
    # Импорт в приложение: диплинк по шаблону — через страницу-переходник (12.4, 12.10)
    SUBSCRIPTION_LINK = "subscriptionLink"
    # Копирование ссылки на подписку (12.4)
    COPY_BUTTON = "copyButton"


class ConfigButton(_ConfigModel):
    link: str = ""
    # Строка, а не перечисление: кнопку неизвестного вида инструкция пропускает
    type: str
    text: Localized = Field(default_factory=dict[str, str])


class ConfigBlock(_ConfigModel):
    """Шаг инструкции: заголовок, описание, кнопки."""

    title: Localized = Field(default_factory=dict[str, str])
    description: Localized = Field(default_factory=dict[str, str])
    buttons: list[ConfigButton] = Field(default_factory=list[ConfigButton])


class ConfigApp(_ConfigModel):
    name: str
    featured: bool = False
    blocks: list[ConfigBlock] = Field(default_factory=list[ConfigBlock])

    def install_links(self) -> list[ConfigButton]:
        """Ссылки на установку приложения — обычные ссылки его шагов (12.12)."""
        return [
            button
            for block in self.blocks
            for button in block.buttons
            if button.type == ButtonType.EXTERNAL and button.link
        ]

    def import_buttons(self) -> list[ConfigButton]:
        """Кнопки импорта в приложение — диплинки по шаблону (12.4)."""
        return [
            button
            for block in self.blocks
            for button in block.buttons
            if button.type == ButtonType.SUBSCRIPTION_LINK and button.link
        ]


class ConfigPlatform(_ConfigModel):
    display_name: Localized = Field(default_factory=dict[str, str])
    apps: list[ConfigApp] = Field(default_factory=list[ConfigApp])

    def recommended(self) -> ConfigApp | None:
        """12.3, решение 0058: первое приложение с признаком «рекомендуемое» в порядке
        конфига, а если таких нет — первое приложение платформы."""
        featured = [app for app in self.apps if app.featured]
        if featured:
            return featured[0]
        return self.apps[0] if self.apps else None

    def app(self, name: str) -> ConfigApp | None:
        return next((app for app in self.apps if app.name == name), None)


class BaseTranslations(_ConfigModel):
    installation_guide_header: Localized = Field(default_factory=dict[str, str])


class GuideConfig(_ConfigModel):
    """Содержимое конфига Subscription Page. Порядок платформ — как в ответе панели."""

    locales: list[str] = Field(default_factory=list[str])
    base_translations: BaseTranslations = Field(default_factory=BaseTranslations)
    platforms: dict[str, ConfigPlatform] = Field(default_factory=dict[str, ConfigPlatform])


class ConfigError(Exception):
    """Конфиг не похож на конфиг Subscription Page."""


def parse_config(raw: str) -> GuideConfig:
    try:
        return GuideConfig.model_validate_json(raw)
    except ValidationError as error:
        raise ConfigError(f"Конфиг Subscription Page не разобран: {error}") from error


class Language:
    """12.2: тексты — на языке клиента; если его нет в конфиге — на языке бота по
    умолчанию; если нет и его — на первом языке конфига."""

    def __init__(self, config: GuideConfig, client: str | None, default: str) -> None:
        primary = (client or "").replace("_", "-").split("-", 1)[0].lower()
        self._order = [code for code in (primary, default, *config.locales[:1]) if code]

    def pick(self, text: Localized) -> str:
        for code in self._order:
            value = text.get(code)
            if value:
                return value
        # Ни одного из языков нет — первый текст, какой есть
        return next((value for value in text.values() if value), "")
