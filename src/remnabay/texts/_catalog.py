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
from functools import cached_property
from importlib.resources import files
from zoneinfo import ZoneInfo

from babel import Locale
from babel.dates import format_date, format_time

_VARIABLE = re.compile(r"\{(\w+)\}")
# Остаток фигурных скобок после переменных: «{brand name}», «{brand_name», «}»
_STRAY_BRACE = re.compile(r"\{[^{}\n]{0,40}\}?|\}")
DATE_FALLBACK_KEY = "date.fallback"
# Метки мест дат в собранном тексте (messaging, `Dates`): символы из области для
# частного использования. В тексте оператора их быть не может — иначе дата встанет
# не туда, поэтому такие тексты не сохраняются
DATE_MARK_START = "\ue000"
DATE_MARK_END = "\ue001"
# Числа, по которым показываются примеры форм множественного числа
_PLURAL_SAMPLE_RANGE = range(201)
# Дата в запасном тексте: число, месяц словом на языке клиента, год — «1 ноября 2026»
_DATE_PATTERN = "d MMMM y"
_TIME_PATTERN = "HH:mm"


class TextError(Exception):
    """Нет такого ключа, не хватает переменной или в правке чужая переменная."""


def variables_of(template: str) -> set[str]:
    return set(_VARIABLE.findall(template))


def variable_names(template: str) -> list[str]:
    """Переменные шаблона в порядке появления, без повторов."""
    return list(dict.fromkeys(_VARIABLE.findall(template)))


def plural_key(unit_key: str, form: str) -> str:
    """Ключ формы множественного числа как отдельного текста: `unit.days.few`."""
    return f"{unit_key}.{form}"


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

    def plural_form(self, unit_key: str, count: int) -> str:
        """Форма единицы для числа по правилам языка; нет такой формы — `other`."""
        forms = self.plurals[unit_key]
        category = Locale.parse(self.language).plural_form(count)
        return category if category in forms else "other"

    @cached_property
    def plural_forms(self) -> dict[str, tuple[str, str]]:
        """Формы множественного числа как отдельные тексты: ключ формы → (единица, форма)."""
        return {
            plural_key(unit_key, form): (unit_key, form)
            for unit_key, forms in self.plurals.items()
            for form in forms
        }

    @cached_property
    def entries(self) -> dict[str, str]:
        """Все тексты, которые оператор может переписать: обычные и формы
        множественного числа (`unit.days.few`), — ключ → текст по умолчанию."""
        result = dict(self.texts)
        for key, (unit_key, form) in self.plural_forms.items():
            result[key] = self.plurals[unit_key][form]
        return result

    def plural_counts(self, key: str) -> list[int]:
        """Целые числа, для которых используется форма (`unit.days.few` → 2, 3, 4, 22…).
        Пусто — форма для целых чисел не используется (у русского `other`)."""
        unit_key, form = self.plural_forms[key]
        return [n for n in _PLURAL_SAMPLE_RANGE if self.plural_form(unit_key, n) == form]


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
            catalog = self._catalogs[language]
            forms = catalog.plurals.get(unit_key)
            if forms is None:
                continue
            form = catalog.plural_form(unit_key, count)
            # Форма, переписанная оператором, важнее формы по умолчанию (1.29)
            template = self._overrides.get((plural_key(unit_key, form), language), forms[form])
            return substitute(template, {"count": str(count)})
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


def allowed_variables(catalogs: Mapping[str, Catalog], key: str) -> list[str]:
    """Переменные, которые можно использовать в тексте: те, что в тексте по умолчанию
    (на любом языке), в порядке появления."""
    defaults = [catalog.entries[key] for catalog in catalogs.values() if key in catalog.entries]
    if not defaults:
        raise TextError(f"Нет текста с ключом {key}")
    return list(dict.fromkeys(name for default in defaults for name in variable_names(default)))


def check_override(catalogs: Mapping[str, Catalog], key: str, text: str) -> None:
    """Правка оператора: ключ существует, текст не пустой, переменные — только из
    текста по умолчанию и записаны правильно (1.29).

    Переменные можно переставлять и убирать, но не придумывать новые
    (`03-bot-texts.md`, «Соглашения»). Тексты бота — обычный текст без разметки
    (форматирование — только сущностями дат), поэтому ошибка «форматирования» — это
    фигурные скобки не вокруг переменной и служебные символы меток дат.
    """
    allowed = set(allowed_variables(catalogs, key))
    if not text.strip():
        raise TextError("Текст пустой — Telegram такое сообщение не отправит")
    if DATE_MARK_START in text or DATE_MARK_END in text:
        raise TextError("В тексте есть служебные символы, которые магазин использует для дат")
    unknown = variables_of(text) - allowed
    if unknown:
        names = ", ".join(f"{{{name}}}" for name in sorted(unknown))
        hint = ", ".join(f"{{{name}}}" for name in sorted(allowed)) or "нет"
        raise TextError(f"В тексте {key} нельзя использовать {names}. Можно: {hint}")
    stray = _STRAY_BRACE.search(_VARIABLE.sub("", text))
    if stray:
        raise TextError(
            f"Фигурные скобки — только вокруг имени переменной, например {{brand_name}}; "
            f"проверьте «{stray.group(0)}»"
        )
