"""Владелец из `.env` (1.2, решение 0036)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import journal
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import Actor, JsonValue, Outcome, Subject

TEAM_MEMBER_SUBJECT = "team_member"


async def ensure_owner(session: AsyncSession, telegram_id: int) -> TeamMember:
    """Telegram ID из `.env` — действующий владелец; остальные участники не меняются.

    Это и первый вход в админку, и путь восстановления доступа: если владельца
    нет в команде, он добавляется; если он помощник — становится владельцем.
    Вызывается при каждом запуске веба; фиксирует вызывающий.
    """
    member = await session.scalar(
        select(TeamMember).where(
            TeamMember.telegram_id == telegram_id, TeamMember.revoked_at.is_(None)
        )
    )
    if member is not None and member.role == TeamRole.OWNER:
        return member

    if member is None:
        member = TeamMember(telegram_id=telegram_id, role=TeamRole.OWNER)
        session.add(member)
        await session.flush()
        action = "team.owner_added"
        details: dict[str, JsonValue] = {"telegram_id": telegram_id}
    else:
        details = {"telegram_id": telegram_id, "old_role": member.role.value}
        member.role = TeamRole.OWNER
        action = "team.owner_restored"

    await journal.record(
        session,
        actor=Actor.SYSTEM,
        action=action,
        outcome=Outcome.SUCCESS,
        subject=Subject(TEAM_MEMBER_SUBJECT, member.id),
        details={**details, "source": "env"},
    )
    return member
