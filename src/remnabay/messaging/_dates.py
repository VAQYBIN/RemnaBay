"""Даты в сообщениях: сущность date_time и запасной текст (03-screens, решение 0044).

Клиент видит дату в своём часовом поясе и привычном формате — её показывает
Telegram по сущности date_time. Запасной текст — дата и время магазина с подписью
пояса (`date.fallback`): его видят клиенты, которые date_time не форматируют.
"""

import re
from dataclasses import dataclass
from datetime import datetime

# Формат даты для клиента: длинная дата и время («1 ноября 2026, 23:00»)
DATE_FORMAT = "Dt"
# Метка места даты в тексте: символы из области для частного использования, в
# текстах оператора их не бывает
_DATE_MARK = "{}"
_DATE_MARK_PATTERN = re.compile("(\\d+)")


def utf16_length(text: str) -> int:
    """Длина в единицах UTF-16 — так Telegram считает смещения сущностей."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class DateValue:
    moment: datetime
    # Запасной текст: во времени магазина с подписью пояса (`date.fallback`)
    fallback: str


@dataclass(frozen=True)
class DateEntity:
    """Сущность date_time: где в тексте дата и какой это момент."""

    offset: int
    length: int
    unix_time: int


class Dates:
    """Даты текста: в текст подставляется метка, при сборке — запасной текст и сущность."""

    def __init__(self) -> None:
        self._values: list[DateValue] = []

    def mark(self, value: DateValue) -> str:
        self._values.append(value)
        return _DATE_MARK.format(len(self._values) - 1)

    def render(self, text: str) -> tuple[str, list[DateEntity]]:
        """Текст с запасными датами и сущности date_time на их местах."""
        parts: list[str] = []
        entities: list[DateEntity] = []
        position = 0
        offset = 0
        for match in _DATE_MARK_PATTERN.finditer(text):
            before = text[position : match.start()]
            parts.append(before)
            offset += utf16_length(before)
            value = self._values[int(match.group(1))]
            parts.append(value.fallback)
            length = utf16_length(value.fallback)
            entities.append(DateEntity(offset, length, int(value.moment.timestamp())))
            offset += length
            position = match.end()
        parts.append(text[position:])
        return "".join(parts), entities
