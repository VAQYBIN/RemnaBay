"""Бренд оператора (решение 0038): название, логотип, фирменные цвета."""

from typing import Annotated

from pydantic import Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.shop_settings import ShopSetting, get_setting

BRAND_NAME = ShopSetting("brand.name", TypeAdapter[str](Annotated[str, Field(max_length=64)]), "")


async def brand_name(session: AsyncSession) -> str:
    """Название бренда; пусто, пока оператор его не задал."""
    return (await get_setting(session, BRAND_NAME)).strip()
