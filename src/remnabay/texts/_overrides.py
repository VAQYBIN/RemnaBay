"""Правки текстов бота оператором: сохранение и сброс к тексту по умолчанию."""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.texts._catalog import check_override, load_default_catalogs
from remnabay.texts._models import BotTextOverride

JOURNAL_SUBJECT = "bot_text"


async def get_override(session: AsyncSession, key: str, language: str) -> str | None:
    return await session.scalar(
        select(BotTextOverride.text).where(
            BotTextOverride.key == key, BotTextOverride.language == language
        )
    )


async def save_override(
    session: AsyncSession, key: str, language: str, text: str, *, member_id: int
) -> None:
    """Текст оператора для ключа и языка. Переменные — только из текста по умолчанию
    (`TextError` иначе). Изменение — в журнал (4.25)."""
    check_override(load_default_catalogs(), key, text)
    old = await get_override(session, key, language)
    await session.execute(
        insert(BotTextOverride)
        .values(key=key, language=language, text=text, updated_by_id=member_id)
        .on_conflict_do_update(
            index_elements=[BotTextOverride.key, BotTextOverride.language],
            set_={"text": text, "updated_by_id": member_id},
        )
    )
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="text.changed",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, f"{key}:{language}"),
        details={"key": key, "language": language, "old": old, "new": text},
    )


async def reset_override(session: AsyncSession, key: str, language: str, *, member_id: int) -> None:
    """Сброс к тексту по умолчанию: правка удаляется."""
    old = await get_override(session, key, language)
    if old is None:
        return
    await session.execute(
        delete(BotTextOverride).where(
            BotTextOverride.key == key, BotTextOverride.language == language
        )
    )
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="text.reset",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, f"{key}:{language}"),
        details={"key": key, "language": language, "old": old},
    )
