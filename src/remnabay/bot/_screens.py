"""Экраны бота: живое меню, кнопки, даты (03-screens, «Принципы бота»).

- Меню живёт в одном сообщении, которое правится при нажатии кнопок; события —
  отдельные сообщения, и нажатие на них открывает меню новым сообщением.
- Даты — сущностью date_time: клиент видит дату в своём часовом поясе; запасной
  текст — во времени магазина с подписью пояса (0044).
- Rich-сообщения — карточка подписки и выбор тарифа (0017).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    MessageEntity,
    RichTextDateTime,
)

from remnabay.bot._context import BotContext
from remnabay.shop_settings import shop_time_zone

# Формат даты для клиента: длинная дата и время («1 ноября 2026, 23:00»)
DATE_FORMAT = "Dt"
# Метка места даты в тексте: символы из области для частного использования, в
# текстах оператора их не бывает
_DATE_MARK = "{}"
_DATE_MARK_PATTERN = re.compile("(\\d+)")
# Кнопка целевого действия — зелёная, не больше одной на экране (03-screens)
SUCCESS = "success"


def _utf16(text: str) -> int:
    """Длина в единицах UTF-16 — так Telegram считает смещения сущностей."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class DateValue:
    moment: datetime
    # Запасной текст: во времени магазина с подписью пояса (`date.fallback`)
    fallback: str


class Dates:
    """Даты экрана: в текст подставляется метка, при сборке — запасной текст и сущность."""

    def __init__(self) -> None:
        self._values: list[DateValue] = []

    def mark(self, value: DateValue) -> str:
        self._values.append(value)
        return _DATE_MARK.format(len(self._values) - 1)

    def entities(self, text: str) -> tuple[str, list[MessageEntity]]:
        """Текст с запасными датами и сущности date_time на их местах."""
        parts: list[str] = []
        entities: list[MessageEntity] = []
        position = 0
        offset = 0
        for match in _DATE_MARK_PATTERN.finditer(text):
            before = text[position : match.start()]
            parts.append(before)
            offset += _utf16(before)
            value = self._values[int(match.group(1))]
            parts.append(value.fallback)
            length = _utf16(value.fallback)
            entities.append(
                MessageEntity(
                    type="date_time",
                    offset=offset,
                    length=length,
                    unix_time=int(value.moment.timestamp()),
                    date_time_format=DATE_FORMAT,
                )
            )
            offset += length
            position = match.end()
        parts.append(text[position:])
        return "".join(parts), entities


async def date_value(ctx: BotContext, moment: datetime) -> DateValue:
    fallback = ctx.texts.date_fallback(
        moment, ctx.client.language_code, await shop_time_zone(ctx.session)
    )
    return DateValue(moment, fallback)


def rich_date(value: DateValue) -> RichTextDateTime:
    return RichTextDateTime(
        text=value.fallback, unix_time=int(value.moment.timestamp()), date_time_format=DATE_FORMAT
    )


@dataclass
class Keyboard:
    """Кнопки экрана — по одной в ряд."""

    rows: list[list[InlineKeyboardButton]] = field(default_factory=list[list[InlineKeyboardButton]])

    def action(self, text: str, data: str, *, style: str | None = None) -> Keyboard:
        self.rows.append([InlineKeyboardButton(text=text, callback_data=data, style=style)])
        return self

    def link(self, text: str, url: str, *, style: str | None = None) -> Keyboard:
        self.rows.append([InlineKeyboardButton(text=text, url=url, style=style)])
        return self

    def copy(self, text: str, value: str) -> Keyboard:
        self.rows.append([InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value))])
        return self

    def markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=self.rows)


@dataclass(frozen=True)
class Screen:
    """Экран: обычный текст с сущностями дат или rich-сообщение, и кнопки."""

    keyboard: Keyboard
    text: str | None = None
    entities: list[MessageEntity] | None = None
    rich: InputRichMessage | None = None

    @classmethod
    def plain(cls, text: str, keyboard: Keyboard, dates: Dates | None = None) -> Screen:
        if dates is None:
            return cls(keyboard=keyboard, text=text)
        rendered, entities = dates.entities(text)
        return cls(keyboard=keyboard, text=rendered, entities=entities or None)

    async def send(self, bot: Bot, chat_id: int) -> None:
        markup = self.keyboard.markup()
        if self.rich is not None:
            await bot.send_rich_message(chat_id, self.rich, reply_markup=markup)
        else:
            await bot.send_message(
                chat_id, self.text or "", entities=self.entities, reply_markup=markup
            )

    async def edit(self, bot: Bot, chat_id: int, message_id: int) -> None:
        """Правит живое меню; если править нельзя (сообщение старое или удалено) —
        присылает экран новым сообщением."""
        markup = self.keyboard.markup()
        try:
            if self.rich is not None:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    rich_message=self.rich,
                    reply_markup=markup,
                )
            else:
                await bot.edit_message_text(
                    self.text or "",
                    chat_id=chat_id,
                    message_id=message_id,
                    entities=self.entities,
                    reply_markup=markup,
                )
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                return
            await self.send(bot, chat_id)
