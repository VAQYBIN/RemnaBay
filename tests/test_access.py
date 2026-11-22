"""Доступ команды к админке: владелец из `.env` (1.2), модель команды (1.3)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access import TEAM_MEMBER_SUBJECT, ensure_owner
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry, Subject, entries_for
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_team_member

OWNER_ID = 100500


async def _active(session: AsyncSession) -> list[TeamMember]:
    result = await session.scalars(
        select(TeamMember).where(TeamMember.revoked_at.is_(None)).order_by(TeamMember.id)
    )
    return list(result)


async def test_1_2_owner_added_on_start(db_session: AsyncSession) -> None:
    """1.2: при первом запуске Telegram ID из `.env` становится владельцем."""
    member = await ensure_owner(db_session, OWNER_ID)

    assert [(m.telegram_id, m.role) for m in await _active(db_session)] == [
        (OWNER_ID, TeamRole.OWNER)
    ]
    entries = await entries_for(db_session, Subject(TEAM_MEMBER_SUBJECT, member.id))
    assert [(e.action, e.details["source"]) for e in entries] == [("team.owner_added", "env")]


async def test_1_2_repeated_start_changes_nothing(db_session: AsyncSession) -> None:
    """1.2: при каждом следующем запуске владелец уже есть — второй не появляется."""
    first = await ensure_owner(db_session, OWNER_ID)
    second = await ensure_owner(db_session, OWNER_ID)

    assert first.id == second.id
    assert len(await _active(db_session)) == 1
    journaled = await db_session.scalars(
        select(JournalEntry.action).where(journaled_in_test(), JournalEntry.action.like("team.%"))
    )
    assert list(journaled) == ["team.owner_added"]


async def test_1_2_other_owners_kept(db_session: AsyncSession) -> None:
    """1.2: другие владельцы не удаляются и не понижаются."""
    other = make_team_member(telegram_id=777, role=TeamRole.OWNER)
    await add(db_session, other)

    await ensure_owner(db_session, OWNER_ID)

    assert {(m.telegram_id, m.role) for m in await _active(db_session)} == {
        (777, TeamRole.OWNER),
        (OWNER_ID, TeamRole.OWNER),
    }


async def test_1_2_assistant_from_env_becomes_owner(db_session: AsyncSession) -> None:
    """1.2: путь восстановления доступа — помощник с Telegram ID из `.env` снова владелец."""
    assistant = make_team_member(telegram_id=OWNER_ID, role=TeamRole.ASSISTANT)
    await add(db_session, assistant)

    member = await ensure_owner(db_session, OWNER_ID)

    assert member.id == assistant.id
    assert member.role == TeamRole.OWNER
    entries = await entries_for(db_session, Subject(TEAM_MEMBER_SUBJECT, member.id))
    assert [(e.action, e.details["old_role"]) for e in entries] == [
        ("team.owner_restored", "assistant")
    ]


async def test_1_2_revoked_owner_from_env_comes_back(db_session: AsyncSession) -> None:
    """1.2: отозванный участник с Telegram ID из `.env` возвращается владельцем."""
    revoked = make_team_member(telegram_id=OWNER_ID, role=TeamRole.OWNER)
    await add(db_session, revoked)
    revoked.revoked_at = revoked.created_at
    await db_session.flush()

    member = await ensure_owner(db_session, OWNER_ID)

    assert member.id != revoked.id
    assert [m.id for m in await _active(db_session)] == [member.id]


async def test_1_3_team_member_stores_role(db_session: AsyncSession) -> None:
    """1.3: модель команды хранит роль участника; в MVP — только владелец из `.env`."""
    member = await ensure_owner(db_session, OWNER_ID)
    db_session.expunge_all()

    stored = await db_session.get_one(TeamMember, member.id)
    assert stored.role == TeamRole.OWNER
