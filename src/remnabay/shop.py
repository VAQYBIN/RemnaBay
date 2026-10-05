"""Состояние магазина: не открыт, открыт, временно закрыт (1.13, 1.14, 1.21, 1.22).

Пока магазин не открыт, бот работает только для команды и тестировщиков — режим
проверки. Временно закрытый магазин бот не прячет: клиенты видят свои подписки,
а покупку, продление и триал блоки 3 и 5 закрывают по `sales_open`.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, PositiveInt, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.shop_settings import ShopSetting, get_setting


class ShopState(StrEnum):
    NOT_OPENED = "not_opened"
    OPEN = "open"
    # Владелец закрыл открытый магазин (1.22)
    PAUSED = "paused"


# Telegram ID тестировщиков; список небольшой — его заполняет владелец руками
MAX_TESTERS = 100

SHOP_STATE = ShopSetting("shop.state", TypeAdapter(ShopState), ShopState.NOT_OPENED)
SHOP_TESTERS = ShopSetting(
    "shop.testers",
    TypeAdapter[list[int]](Annotated[list[PositiveInt], Field(max_length=MAX_TESTERS)]),
    [],
)
# Контакт поддержки (чек-лист); пока пусто, кнопка «Поддержка» не показывается
SHOP_SUPPORT_CONTACT = ShopSetting(
    "shop.support_contact", TypeAdapter[str](Annotated[str, Field(max_length=256)]), ""
)


async def shop_state(session: AsyncSession) -> ShopState:
    return await get_setting(session, SHOP_STATE)


async def sales_open(session: AsyncSession) -> bool:
    """Можно ли покупать, продлевать и брать триал (1.22): только в открытом магазине."""
    return await shop_state(session) == ShopState.OPEN


async def is_tester(session: AsyncSession, telegram_id: int) -> bool:
    return telegram_id in await get_setting(session, SHOP_TESTERS)
