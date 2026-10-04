"""Тексты бота по ключу и языку (решение 0018).

Тексты по умолчанию — файлы `defaults/<язык>.toml`, а не строки в коде. Оператор
может переписать любой текст; правки хранятся в базе (`BotTextOverride`).

Переменные `{имя}` подставляет магазин. Подстановка — только по имени: никаких
выражений вроде `{x.attr}` в шаблонах, которые редактирует оператор.
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from zoneinfo import ZoneInfo

from babel import Locale
from babel.dates import format_date, format_time

_VARIABLE = re.compile(r"\{(\w+)\}")
DATE_FALLBACK_KEY = "date.fallback"
# Дата в запасном тексте: число, месяц словом на языке клиента, год — «1 ноября 2026»
_DATE_PATTERN = "d MMMM y"
_TIME_PATTERN = "HH:mm"


class TextError(Exception):
    """Нет такого ключа, не хватает переменной или в правке чужая переменная."""


def variables_of(template: str) -> set[str]:
    return set(_VARIABLE.findall(template))


def substitute(template: str, variables: Mapping[str, str]) -> str:
    """Подставляет переменные шаблона; лишние переменные игнорируются."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise TextError(f"Не передана переменная {{{name}}}")
        return variables[name]

    return _VARIABLE.sub(value, template)


@dataclass(frozen=True)
class Catalog:
    """Тексты одного языка: обычные и с формами множественного числа."""

    language: str
    texts: Mapping[str, str]
    plurals: Mapping[str, Mapping[str, str]]


def load_default_catalogs() -> dict[str, Catalog]:
    """Тексты по умолчанию всех языков, для которых есть файл."""
    catalogs: dict[str, Catalog] = {}
    for resource in files("remnabay.texts").joinpath("defaults").iterdir():
        if not resource.name.endswith(".toml"):
            continue
        language = resource.name.removesuffix(".toml")
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
        plurals = data.pop("plural", {})
        catalogs[language] = Catalog(language=language, texts=data, plurals=plurals)
    return catalogs


def _primary(language: str) -> str:
    return language.replace("_", "-").split("-", 1)[0].lower()


class Texts:
    """Выбор текста: язык клиента, затем язык бота по умолчанию (0018).

    Для каждого языка правка оператора важнее текста по умолчанию.
    """

    def __init__(
        self,
        catalogs: Mapping[str, Catalog],
        overrides: Mapping[tuple[str, str], str],
        default_language: str,
    ) -> None:
        if default_language not in catalogs:
            raise TextError(f"Нет текстов для языка по умолчанию {default_language}")
        self._catalogs = catalogs
        self._overrides = overrides
        self._default_language = default_language

    def language_for(self, client_language: str | None) -> str:
        """Язык, на котором отвечать клиенту: его, если есть перевод, иначе по умолчанию."""
        if client_language:
            primary = _primary(client_language)
            if primary in self._catalogs:
                return primary
        return self._default_language

    def _languages(self, client_language: str | None) -> list[str]:
        chosen = self.language_for(client_language)
        return [chosen] if chosen == self._default_language else [chosen, self._default_language]

    def template(self, key: str, client_language: str | None) -> str:
        for language in self._languages(client_language):
            override = self._overrides.get((key, language))
            if override is not None:
                return override
            default = self._catalogs[language].texts.get(key)
            if default is not None:
                return default
        raise TextError(f"Нет текста с ключом {key}")

    def render(self, key: str, client_language: str | None, /, **variables: str) -> str:
        return substitute(self.template(key, client_language), variables)

    def quantity(self, unit_key: str, client_language: str | None, count: int) -> str:
        """Число с единицей по правилам множественного числа языка: «3 дня», «5 дней»."""
        for language in self._languages(client_language):
            forms = self._catalogs[language].plurals.get(unit_key)
            if forms is None:
                continue
            category = Locale.parse(language).plural_form(count)
            form = forms.get(category) or forms["other"]
            return substitute(form, {"count": str(count)})
        raise TextError(f"Нет форм множественного числа для {unit_key}")

    def date_fallback(
        self, moment: datetime, client_language: str | None, shop_time_zone: ZoneInfo
    ) -> str:
        """Запасной текст даты: во времени магазина, месяц — на языке клиента (0044)."""
        language = self.language_for(client_language)
        local = moment.astimezone(shop_time_zone)
        return self.render(
            DATE_FALLBACK_KEY,
            language,
            date=format_date(local, _DATE_PATTERN, locale=language),
            time=format_time(local, _TIME_PATTERN, locale=language),
        )


def check_override(catalogs: Mapping[str, Catalog], key: str, text: str) -> None:
    """Правка оператора: ключ существует, переменные — только из текста по умолчанию.

    Переменные можно переставлять и убирать, но не придумывать новые
    (`03-bot-texts.md`, «Соглашения»).
    """
    defaults = [catalog.texts[key] for catalog in catalogs.values() if key in catalog.texts]
    if not defaults:
        raise TextError(f"Нет текста с ключом {key}")
    allowed = {name for default in defaults for name in variables_of(default)}
    unknown = variables_of(text) - allowed
    if unknown:
        names = ", ".join(f"{{{name}}}" for name in sorted(unknown))
        raise TextError(f"В тексте {key} нельзя использовать {names}")
