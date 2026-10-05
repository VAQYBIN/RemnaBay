"""Настройки «Бренд» (04-operator-settings.md, решение 0038)."""

from typing import Annotated

from pydantic import AfterValidator, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand._color import Rgb
from remnabay.brand._theme import REMNABAY_PRIMARY
from remnabay.shop_settings import ShopSetting, get_setting

MAX_NAME_LENGTH = 64


def _color(value: str) -> str:
    """Цвет — `#rrggbb` в нижнем регистре."""
    return Rgb.from_hex(value).to_hex()


type Color = Annotated[str, AfterValidator(_color)]

BRAND_NAME = ShopSetting(
    "brand.name", TypeAdapter[str](Annotated[str, Field(max_length=MAX_NAME_LENGTH)]), ""
)
# Основной фирменный цвет: обязателен, по умолчанию — цвет RemnaBay
BRAND_PRIMARY_COLOR = ShopSetting(
    "brand.primary_color", TypeAdapter[str](Color), REMNABAY_PRIMARY.lower()
)
# Дополнительный: по желанию; без него акценты — из шкалы основного (0038)
BRAND_SECONDARY_COLOR = ShopSetting(
    "brand.secondary_color", TypeAdapter[str | None](Color | None), None
)
# Приветственный текст — текст бота с этим ключом (03-bot-texts.md, З1)
WELCOME_TEXT_KEY = "welcome_text"


async def brand_name(session: AsyncSession) -> str:
    """Название бренда; пусто, пока оператор его не задал."""
    return (await get_setting(session, BRAND_NAME)).strip()
