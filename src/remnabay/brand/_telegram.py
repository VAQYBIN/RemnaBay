"""«Применить в Telegram»: имя, описания и аватар бота из бренда (1.27, решение 0058).

- Имя бота — название бренда; меню команд — `/start` и `/help` (решение 0060).
  Описание (до 512 знаков, в пустом чате до `/start`) и
  «Коротко о боте» (до 120 знаков, в профиле) — поля «Бренда»; пустое поле в
  Telegram не меняется. В MVP один язык — всё задаётся боту для всех языков.
- Аватар — квадратный знак в выбранном масштабе на непрозрачном фоне: фирменный
  цвет, белый или чёрный; клиенты видят его кругом (решение 0060). Telegram
  принимает аватар только в JPG, а знак хранится как SVG или PNG: картинку собирает
  админка в браузере по тому же предпросмотру, который видит владелец (решение
  0059), а магазин проверяет, что это квадратный JPG, и отправляет его в Telegram.
- Результат и ошибка Telegram видны в настройках и пишутся в журнал.
"""

import struct
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from aiogram import Bot
from aiogram.exceptions import AiogramError
from aiogram.types import BotCommand, BufferedInputFile, InputProfilePhotoStatic
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand._assets import AssetKind, asset_versions
from remnabay.brand._settings import brand_name
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import load_texts
from remnabay.shop_settings import SHOP_LANGUAGE, ShopSetting, get_setting, remember_setting

JOURNAL_SUBJECT = "telegram_bot"
MAX_DESCRIPTION_LENGTH = 512
MAX_SHORT_DESCRIPTION_LENGTH = 120
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MIN_AVATAR_PIXELS = 160
MAX_AVATAR_PIXELS = 2048
_JPEG_START = b"\xff\xd8\xff"
# Маркеры начала кадра JPEG (SOF0–SOF15, кроме DHT, JPG и DAC): в них размер картинки
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Команды в меню бота; описания — тексты `command.<команда>`
_COMMANDS = ("start", "help")

BOT_DESCRIPTION = ShopSetting(
    "brand.bot_description",
    TypeAdapter[str](Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH)]),
    "",
)
BOT_SHORT_DESCRIPTION = ShopSetting(
    "brand.bot_short_description",
    TypeAdapter[str](Annotated[str, Field(max_length=MAX_SHORT_DESCRIPTION_LENGTH)]),
    "",
)


class AvatarBackground(StrEnum):
    """Фон аватара — выбирается в окне применения по круглому предпросмотру."""

    BRAND = "brand"
    WHITE = "white"
    BLACK = "black"


class TelegramApplied(BaseModel):
    """Итог последнего «Применить в Telegram» — виден в настройках."""

    at: datetime
    ok: bool
    background: AvatarBackground
    # Ошибка Telegram, если применить не удалось
    error: str | None = None


TELEGRAM_APPLIED = ShopSetting(
    "brand.telegram_applied", TypeAdapter[TelegramApplied | None](TelegramApplied | None), None
)


class TelegramApplyError(Exception):
    """Применить нельзя: не заданы название или знак, аватар не подходит."""


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Размер JPEG по маркеру начала кадра."""
    if not data.startswith(_JPEG_START):
        raise TelegramApplyError("Аватар — картинка JPG")
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            break
        marker = data[position + 1]
        length = struct.unpack(">H", data[position + 2 : position + 4])[0]
        if marker in _SOF_MARKERS and position + 9 <= len(data):
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            return width, height
        position += 2 + length
    raise TelegramApplyError("Аватар — картинка JPG")


def check_avatar(data: bytes) -> None:
    if len(data) > MAX_AVATAR_BYTES:
        raise TelegramApplyError("Аватар больше 2 МБ")
    width, height = _jpeg_size(data)
    if width != height:
        raise TelegramApplyError("Аватар должен быть квадратным")
    if not MIN_AVATAR_PIXELS <= width <= MAX_AVATAR_PIXELS:
        raise TelegramApplyError(
            f"Аватар — от {MIN_AVATAR_PIXELS} до {MAX_AVATAR_PIXELS} пикселей по стороне"
        )


async def _commands(session: AsyncSession) -> list[BotCommand]:
    texts = await load_texts(session)
    language = await get_setting(session, SHOP_LANGUAGE)
    return [
        BotCommand(command=command, description=texts.render(f"command.{command}", language))
        for command in _COMMANDS
    ]


async def apply_to_telegram(
    session: AsyncSession,
    bot: Bot,
    avatar: bytes,
    background: AvatarBackground,
    *,
    member_id: int,
) -> TelegramApplied:
    """Задаёт боту имя, описания и аватар. Кнопка доступна, когда заданы название и
    квадратный знак (1.27). Ошибка Telegram не исключение: она в итоге и в журнале."""
    name = await brand_name(session)
    if not name or AssetKind.MARK not in await asset_versions(session):
        raise TelegramApplyError("Сначала задайте название и квадратный знак")
    check_avatar(avatar)
    description = (await get_setting(session, BOT_DESCRIPTION)).strip()
    short = (await get_setting(session, BOT_SHORT_DESCRIPTION)).strip()
    done: list[str] = []
    error: str | None = None
    try:
        await bot.set_my_name(name=name)
        done.append("name")
        # Меню команд: /start и /help; /admin скрыта (03-screens, решение 0060)
        await bot.set_my_commands(commands=await _commands(session))
        done.append("commands")
        # Пустое поле в Telegram не меняется
        if description:
            await bot.set_my_description(description=description)
            done.append("description")
        if short:
            await bot.set_my_short_description(short_description=short)
            done.append("short_description")
        photo = InputProfilePhotoStatic(photo=BufferedInputFile(avatar, filename="avatar.jpg"))
        await bot.set_my_profile_photo(photo=photo)
        done.append("photo")
    except AiogramError as failure:
        error = str(failure)
    result = TelegramApplied(
        at=datetime.now(UTC), ok=error is None, background=background, error=error
    )
    await remember_setting(session, TELEGRAM_APPLIED, result)
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="brand.applied_to_telegram",
        outcome=Outcome.SUCCESS if error is None else Outcome.FAILURE,
        subject=Subject(JOURNAL_SUBJECT, "profile"),
        details={"background": background.value, "done": list(done), "error": error},
    )
    return result
