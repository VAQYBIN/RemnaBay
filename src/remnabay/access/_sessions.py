"""Сессии веб-админки. В базе — только хэш токена; отзыв доступа завершает сессию сразу."""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import journal
from remnabay.access._owner import TEAM_MEMBER_SUBJECT
from remnabay.access._settings import SESSION_TTL
from remnabay.domain.team import AdminSession, LoginMethod, TeamMember
from remnabay.journal import Actor, Outcome, Subject
from remnabay.shop_settings import get_setting

_TOKEN_BYTES = 32


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_session(
    session: AsyncSession, member: TeamMember, method: LoginMethod, *, now: datetime
) -> tuple[str, datetime]:
    """Новая сессия участника: токен для cookie и срок. Вход пишется в журнал."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = now + await get_setting(session, SESSION_TTL)
    session.add(
        AdminSession(token_hash=_hash(token), team_member_id=member.id, expires_at=expires_at)
    )
    await journal.record(
        session,
        actor=Actor.team_member(member.id),
        action="team.login",
        outcome=Outcome.SUCCESS,
        subject=Subject(TEAM_MEMBER_SUBJECT, member.id),
        details={"method": method.value},
    )
    return token, expires_at


async def member_for_session(
    session: AsyncSession, token: str, *, now: datetime
) -> TeamMember | None:
    """Участник действующей сессии. Отозванный участник — `None` сразу, без ожидания
    конца сессии (01-domain, «Отзыв доступа срабатывает сразу»)."""
    return await session.scalar(
        select(TeamMember)
        .join(AdminSession, AdminSession.team_member_id == TeamMember.id)
        .where(
            AdminSession.token_hash == _hash(token),
            AdminSession.ended_at.is_(None),
            AdminSession.expires_at > now,
            TeamMember.revoked_at.is_(None),
        )
    )


async def end_session(session: AsyncSession, token: str, *, now: datetime) -> None:
    """Выход: сессия завершается, запись в журнал."""
    admin_session = await session.scalar(
        select(AdminSession).where(
            AdminSession.token_hash == _hash(token), AdminSession.ended_at.is_(None)
        )
    )
    if admin_session is None:
        return
    admin_session.ended_at = now
    await journal.record(
        session,
        actor=Actor.team_member(admin_session.team_member_id),
        action="team.logout",
        outcome=Outcome.SUCCESS,
        subject=Subject(TEAM_MEMBER_SUBJECT, admin_session.team_member_id),
    )
