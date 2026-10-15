"""Содержимое и сегмент рассылки — то, что хранится в `Broadcast` (11.1, 11.2)."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# Разметка длиннее самого текста: теги и мнемоники не считаются в 4096 знаков
MAX_MARKUP_LENGTH = 20_000
# До трёх кнопок-ссылок с подписью владельца (11.1)
MAX_LINKS = 3
# Подпись кнопки Telegram показывает в одну строку
MAX_BUTTON_TEXT = 64
MAX_URL_LENGTH = 2048


class LinkButton(BaseModel):
    """Кнопка-ссылка с подписью владельца."""

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_BUTTON_TEXT)
    ]
    # Ссылки, которые Telegram открывает из кнопки (как шаги инструкции, решение 0062)
    url: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, max_length=MAX_URL_LENGTH, pattern=r"^(https?|tg)://\S+$"
        ),
    ]


class BroadcastContent(BaseModel):
    """Текст в разметке Telegram и кнопки; изображение хранится отдельно (`BroadcastImage`).
    «Главное меню» добавляется всегда и не хранится (11.1)."""

    text: Annotated[str, Field(max_length=MAX_MARKUP_LENGTH)] = ""
    buy: bool = False
    renew: bool = False
    links: Annotated[list[LinkButton], Field(max_length=MAX_LINKS)] = []


class SegmentKind(StrEnum):
    """Сегменты MVP (11.2). Новый сегмент — новое значение и его фильтр в `_segments`."""

    ALL = "all"
    ACTIVE = "active"
    EXPIRED = "expired"


class Segment(BaseModel):
    """Описание сегмента, которое хранит рассылка, — а не список условий в коде (0065)."""

    kind: SegmentKind = SegmentKind.ALL
