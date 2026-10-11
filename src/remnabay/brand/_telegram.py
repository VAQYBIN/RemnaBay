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

import hashlib
import struct
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from aiogram import Bot
from aiogram.exceptions import AiogramError, TelegramRetryAfter
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


class AvatarPlacement(BaseModel):
    """Положение знака на аватаре, как его настроил владелец (0060): масштаб — доля
    стороны аватара, %; сдвиг от центра — в пикселях аватара 640×640; поворот — в
    градусах; отражение. Сохраняется, чтобы в следующий раз править, а не настраивать
    заново."""

    scale: Annotated[int, Field(ge=20, le=150)] = 58
    x: Annotated[float, Field(ge=-640, le=640)] = 0
    y: Annotated[float, Field(ge=-640, le=640)] = 0
    rotation: Annotated[int, Field(ge=-180, le=180)] = 0
    flip_x: bool = False
    flip_y: bool = False


class ApplyStep(StrEnum):
    """Что «Применить в Telegram» задаёт боту — шаги независимы друг от друга."""

    NAME = "name"
    COMMANDS = "commands"
    DESCRIPTION = "description"
    SHORT_DESCRIPTION = "short_description"
    PHOTO = "photo"


class StepFailure(BaseModel):
    """Шаг не удался: текст ошибки Telegram и, если Telegram ограничил частоту
    изменений, через сколько секунд можно повторить."""

    step: ApplyStep
    error: str
    retry_after: int | None = None


class TelegramApplied(BaseModel):
    """Итог последнего «Применить в Telegram» — виден в настройках; фон и положение
    знака — начальные значения окна применения в следующий раз."""

    at: datetime
    ok: bool
    background: AvatarBackground
    placement: AvatarPlacement = AvatarPlacement()
    # Что изменено, что уже совпадало с Telegram и что не удалось (0060)
    done: list[ApplyStep] = []
    unchanged: list[ApplyStep] = []
    failed: list[StepFailure] = []
    # SHA-256 аватара, который Telegram последним принял: тот же аватар второй раз
    # не загружается
    avatar_sha256: str | None = None
    # Первая ошибка Telegram — кратко
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


async def _matches[T](read: Awaitable[T], expected: Callable[[T], bool]) -> bool:
    """Совпадает ли то, что уже задано у бота. Telegram не ответил — считаем, что нет:
    тогда значение просто задаётся."""
    try:
        return expected(await read)
    except AiogramError:
        return False


def _failure(step: ApplyStep, error: AiogramError) -> StepFailure:
    retry_after = error.retry_after if isinstance(error, TelegramRetryAfter) else None
    return StepFailure(step=step, error=str(error), retry_after=retry_after)


async def apply_to_telegram(
    session: AsyncSession,
    bot: Bot,
    avatar: bytes,
    background: AvatarBackground,
    placement: AvatarPlacement,
    *,
    member_id: int,
) -> TelegramApplied:
    """Задаёт боту имя, меню команд, описания и аватар (1.27).

    Меняется только то, что отличается (0060): имя, описания и команды сверяются с
    тем, что уже задано у бота в Telegram, аватар — с последним принятым. У Telegram
    жёсткие ограничения частоты изменений (имя — несколько раз в сутки), и
    оператор, который поменял только логотип, не должен упираться в лимит имени.
    Шаги независимы: ошибка одного не мешает остальным. Ошибка Telegram не
    исключение: она в итоге и в журнале."""
    name = await brand_name(session)
    if not name or AssetKind.MARK not in await asset_versions(session):
        raise TelegramApplyError("Сначала задайте название и квадратный знак")
    check_avatar(avatar)
    description = (await get_setting(session, BOT_DESCRIPTION)).strip()
    short = (await get_setting(session, BOT_SHORT_DESCRIPTION)).strip()
    commands = await _commands(session)
    previous = await get_setting(session, TELEGRAM_APPLIED)
    avatar_sha256 = hashlib.sha256(avatar).hexdigest()
    last_avatar = previous.avatar_sha256 if previous is not None else None

    steps: list[tuple[ApplyStep, Awaitable[bool], Callable[[], Awaitable[object]]]] = [
        (
            ApplyStep.NAME,
            _matches(bot.get_my_name(), lambda current: current.name == name),
            lambda: bot.set_my_name(name=name),
        ),
        (
            ApplyStep.COMMANDS,
            _matches(
                bot.get_my_commands(),
                lambda current: (
                    [(c.command, c.description) for c in current]
                    == [(c.command, c.description) for c in commands]
                ),
            ),
            lambda: bot.set_my_commands(commands=commands),
        ),
    ]
    # Пустое поле в Telegram не меняется
    if description:
        steps.append(
            (
                ApplyStep.DESCRIPTION,
                _matches(
                    bot.get_my_description(), lambda current: current.description == description
                ),
                lambda: bot.set_my_description(description=description),
            )
        )
    if short:
        steps.append(
            (
                ApplyStep.SHORT_DESCRIPTION,
                _matches(
                    bot.get_my_short_description(),
                    lambda current: current.short_description == short,
                ),
                lambda: bot.set_my_short_description(short_description=short),
            )
        )
    photo = InputProfilePhotoStatic(photo=BufferedInputFile(avatar, filename="avatar.jpg"))
    steps.append(
        (
            ApplyStep.PHOTO,
            _already(avatar_sha256 == last_avatar),
            lambda: bot.set_my_profile_photo(photo=photo),
        )
    )

    done: list[ApplyStep] = []
    unchanged: list[ApplyStep] = []
    failed: list[StepFailure] = []
    for step, matches, apply in steps:
        if await matches:
            unchanged.append(step)
            continue
        try:
            await apply()
        except AiogramError as error:
            failed.append(_failure(step, error))
        else:
            done.append(step)
    photo_set = ApplyStep.PHOTO in done or ApplyStep.PHOTO in unchanged
    result = TelegramApplied(
        at=datetime.now(UTC),
        ok=not failed,
        background=background,
        placement=placement,
        done=done,
        unchanged=unchanged,
        failed=failed,
        avatar_sha256=avatar_sha256 if photo_set else last_avatar,
        error=failed[0].error if failed else None,
    )
    await remember_setting(session, TELEGRAM_APPLIED, result)
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="brand.applied_to_telegram",
        outcome=Outcome.SUCCESS if not failed else Outcome.FAILURE,
        subject=Subject(JOURNAL_SUBJECT, "profile"),
        details={
            "background": background.value,
            "done": [step.value for step in done],
            "unchanged": [step.value for step in unchanged],
            "failed": [failure.model_dump(mode="json") for failure in failed],
        },
    )
    return result


async def _already(value: bool) -> bool:
    return value
