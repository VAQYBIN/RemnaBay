"""Тексты бота по ключу и языку (решение 0018). Строк, которые видит клиент, в коде нет."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.texts._catalog import (
    DATE_FALLBACK_KEY,
    Catalog,
    TextError,
    Texts,
    check_override,
    load_default_catalogs,
    substitute,
    variables_of,
)
from remnabay.texts._models import BotTextOverride
from remnabay.texts._overrides import get_override, reset_override, save_override


async def load_overrides(session: AsyncSession) -> dict[tuple[str, str], str]:
    """Правки оператора: (ключ, язык) → текст."""
    rows = await session.execute(
        select(BotTextOverride.key, BotTextOverride.language, BotTextOverride.text)
    )
    return {(key, language): text for key, language, text in rows}


__all__ = [
    "DATE_FALLBACK_KEY",
    "BotTextOverride",
    "Catalog",
    "TextError",
    "Texts",
    "check_override",
    "get_override",
    "load_default_catalogs",
    "load_overrides",
    "reset_override",
    "save_override",
    "substitute",
    "variables_of",
]
