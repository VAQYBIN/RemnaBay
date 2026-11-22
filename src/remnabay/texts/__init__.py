"""Тексты бота по ключу и языку (решение 0018). Строк, которые видит клиент, в коде нет."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.texts._catalog import (
    DATE_FALLBACK_KEY,
    DATE_MARK_END,
    DATE_MARK_START,
    Catalog,
    TextError,
    Texts,
    allowed_variables,
    check_override,
    load_default_catalogs,
    plural_key,
    substitute,
    variable_names,
    variables_of,
)
from remnabay.texts._groups import (
    GROUPS,
    MAX_COMMAND_LENGTH,
    MAX_MESSAGE_LENGTH,
    TextGroup,
    TextKind,
    group_of,
    kind_of,
    max_length,
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
    "DATE_MARK_END",
    "DATE_MARK_START",
    "GROUPS",
    "MAX_COMMAND_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "BotTextOverride",
    "Catalog",
    "TextError",
    "TextGroup",
    "TextKind",
    "Texts",
    "allowed_variables",
    "check_override",
    "get_override",
    "group_of",
    "kind_of",
    "load_default_catalogs",
    "load_overrides",
    "max_length",
    "plural_key",
    "reset_override",
    "save_override",
    "substitute",
    "variable_names",
    "variables_of",
]
