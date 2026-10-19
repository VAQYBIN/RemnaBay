"""Экраны бота: живое меню, кнопки, даты (03-screens, «Принципы бота»).

- Меню живёт в одном сообщении, которое правится при нажатии кнопок; события —
  отдельные сообщения, и нажатие на них открывает меню новым сообщением.
- Даты — сущностью date_time: клиент видит дату в своём часовом поясе; запасной
  текст — во времени магазина с подписью пояса (0044).
- Rich-сообщения — карточка подписки и выбор тарифа (0017).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import batched

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockButtons,
    InputRichMessage,
    MessageEntity,
    RichMessageButton,
    RichTextDateTime,
    RichTextUnion,
)

from remnabay.bot._context import BotContext
from remnabay.messaging import DATE_FORMAT, Dates, DateValue, unix_time
from remnabay.shop_settings import shop_time_zone

# Кнопка целевого действия — зелёная, не больше одной на экране (03-screens)
SUCCESS = "success"
# Разрушительное действие — красная («Удалить устройство», 03-screens)
DANGER = "danger"
# Сколько осталось до даты — относительно, «через 21 день» (решение 0060)
RELATIVE_FORMAT = "r"


def message_entities(dates: Dates, text: str) -> tuple[str, list[MessageEntity]]:
    """Текст с запасными датами и сущности date_time на их местах."""
    rendered, found = dates.render(text)
    entities = [
        MessageEntity(
            type="date_time",
            offset=date.offset,
            length=date.length,
            unix_time=date.unix_time,
            date_time_format=DATE_FORMAT,
        )
        for date in found
    ]
    return rendered, entities


async def date_value(ctx: BotContext, moment: datetime) -> DateValue:
    fallback = ctx.texts.date_fallback(
        moment, ctx.client.language_code, await shop_time_zone(ctx.session)
    )
    return DateValue(moment, fallback)


def rich_date(value: DateValue) -> RichTextUnion:
    """Дата сущностью; позже предела Telegram — запасным текстом (`unix_time`)."""
    moment = unix_time(value.moment)
    if moment is None:
        return value.fallback
    return RichTextDateTime(text=value.fallback, unix_time=moment, date_time_format=DATE_FORMAT)


def rich_relative(moment: datetime, fallback: str) -> RichTextUnion:
    """«Через 21 день» в часовом поясе клиента; запасной текст — число дней (0060)."""
    time = unix_time(moment)
    if time is None:
        return fallback
    return RichTextDateTime(text=fallback, unix_time=time, date_time_format=RELATIVE_FORMAT)


def rich_action(text: str, data: str, *, style: str | None = None) -> RichMessageButton:
    """Кнопка внутри rich-сообщения (0044): нажатие разбирает бот."""
    return RichMessageButton(text=text, callback_data=data, style=style)


def rich_link(text: str, url: str) -> RichMessageButton:
    return RichMessageButton(text=text, url=url)


def rich_copy(text: str, value: str) -> RichMessageButton:
    return RichMessageButton(text=text, copy_text=CopyTextButton(text=value))


def button_rows(buttons: Iterable[RichMessageButton], per_row: int) -> list[InputRichBlockButtons]:
    """Кнопки внутри rich-сообщения рядами по `per_row` (не больше 8 — Bot API)."""
    return [
        InputRichBlockButtons(buttons=list(row)) for row in batched(buttons, per_row, strict=False)
    ]


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
        rendered, entities = message_entities(dates, text)
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
