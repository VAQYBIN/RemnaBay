"""Вход в админку через Telegram (1.4–1.6, решение 0051).

По кнопке на странице входа браузер получает ссылку на бот и код. Бот присылает
участнику команды подтверждение с тем же кодом; после нажатия «Подтвердить вход»
браузер при следующем опросе получает сессию. Код защищает от чужой ссылки: если
злоумышленник пришлёт владельцу ссылку своего запроса, коды на его странице и в
боте владельца совпадут только у злоумышленника — владелец увидит чужой код.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import journal
from remnabay.access._owner import TEAM_MEMBER_SUBJECT
from remnabay.access._settings import LOGIN_TTL
from remnabay.domain.team import LoginMethod, LoginRequest, LoginStatus, TeamMember
from remnabay.journal import Actor, Outcome, Subject
from remnabay.shop_settings import get_setting

# Ссылка на бот: t.me/<бот>?start=login_<токен>; параметр start — до 64 символов
START_PREFIX = "login_"
_TOKEN_BYTES = 24
_CODE_DIGITS = 4
# Истёкшие запросы хранятся сутки, затем удаляются при создании новых
_KEEP_EXPIRED = timedelta(days=1)


def is_login_link(text: str | None) -> bool:
    """Сообщение боту — ссылка входа со страницы админки."""
    return text is not None and text.startswith(f"/start {START_PREFIX}")


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _new_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(_CODE_DIGITS))


async def active_member(session: AsyncSession, telegram_id: int) -> TeamMember | None:
    return await session.scalar(
        select(TeamMember).where(
            TeamMember.telegram_id == telegram_id, TeamMember.revoked_at.is_(None)
        )
    )


@dataclass(frozen=True)
class NewLogin:
    """Запрос входа для браузера: ссылка на бот, ключ опроса и код."""

    start_parameter: str
    poll_key: str
    code: str
    expires_at: datetime


async def start_bot_login(session: AsyncSession, *, now: datetime) -> NewLogin:
    await session.execute(delete(LoginRequest).where(LoginRequest.expires_at < now - _KEEP_EXPIRED))
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    poll_key = secrets.token_urlsafe(_TOKEN_BYTES)
    code = _new_code()
    request = LoginRequest(
        method=LoginMethod.BOT_CONFIRM,
        status=LoginStatus.PENDING,
        token_hash=_hash(token),
        poll_hash=_hash(poll_key),
        code=code,
        expires_at=now + await get_setting(session, LOGIN_TTL),
    )
    session.add(request)
    await session.flush()
    return NewLogin(
        start_parameter=START_PREFIX + token,
        poll_key=poll_key,
        code=code,
        expires_at=request.expires_at,
    )


@dataclass(frozen=True)
class LoginOpened:
    """Участник команды открыл ссылку: бот просит подтвердить вход с кодом."""

    request_id: int
    code: str


class LoginClosed(StrEnum):
    # Запрос не найден, истёк, уже использован или открыт другим аккаунтом
    EXPIRED = "expired"
    # Аккаунт не из команды (1.6)
    REJECTED = "rejected"


def _pending(request: LoginRequest | None, now: datetime) -> bool:
    return (
        request is not None
        and request.method == LoginMethod.BOT_CONFIRM
        and request.status == LoginStatus.PENDING
        and request.expires_at > now
    )


async def open_bot_login(
    session: AsyncSession, start_parameter: str, telegram_id: int, *, now: datetime
) -> LoginOpened | LoginClosed:
    """Человек открыл ссылку входа в боте."""
    token = start_parameter.removeprefix(START_PREFIX)
    request = await session.scalar(
        select(LoginRequest).where(LoginRequest.token_hash == _hash(token)).with_for_update()
    )
    if request is None or not _pending(request, now):
        return LoginClosed.EXPIRED
    if request.telegram_id is not None and request.telegram_id != telegram_id:
        return LoginClosed.EXPIRED
    if await member_for_login(session, telegram_id, LoginMethod.BOT_CONFIRM.value) is None:
        request.status = LoginStatus.REJECTED
        request.telegram_id = telegram_id
        return LoginClosed.REJECTED
    request.telegram_id = telegram_id
    return LoginOpened(request_id=request.id, code=request.code or "")


async def confirm_bot_login(
    session: AsyncSession, request_id: int, telegram_id: int, *, now: datetime
) -> bool:
    """Участник нажал «Подтвердить вход». Подтвердить может только тот, кто открыл
    ссылку, и только пока запрос действует (1.5) и участник в команде."""
    request = await session.get(LoginRequest, request_id, with_for_update=True)
    if request is None or not _pending(request, now):
        return False
    member = await active_member(session, telegram_id)
    if request.telegram_id != telegram_id or member is None:
        return False
    request.status = LoginStatus.CONFIRMED
    request.team_member_id = member.id
    request.confirmed_at = now
    return True


class PollStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SIGNED_IN = "signed_in"


@dataclass(frozen=True)
class PollResult:
    status: PollStatus
    member: TeamMember | None = None


async def poll_bot_login(session: AsyncSession, poll_key: str, *, now: datetime) -> PollResult:
    """Браузер спрашивает, подтверждён ли вход. Подтверждённый запрос отдаёт участника
    один раз (1.5): затем он использован."""
    request = await session.scalar(
        select(LoginRequest).where(LoginRequest.poll_hash == _hash(poll_key)).with_for_update()
    )
    if request is None or request.status == LoginStatus.USED or request.expires_at <= now:
        return PollResult(PollStatus.EXPIRED)
    if request.status == LoginStatus.REJECTED:
        return PollResult(PollStatus.REJECTED)
    if request.status == LoginStatus.PENDING:
        return PollResult(PollStatus.PENDING)
    member = await session.get(TeamMember, request.team_member_id)
    if member is None or member.revoked_at is not None:
        return PollResult(PollStatus.EXPIRED)
    request.status = LoginStatus.USED
    request.used_at = now
    return PollResult(PollStatus.SIGNED_IN, member)


async def member_for_login(
    session: AsyncSession, telegram_id: int, method: str
) -> TeamMember | None:
    """Участник команды с этим Telegram ID; чужой аккаунт — отказ с записью в журнал (1.6)."""
    member = await active_member(session, telegram_id)
    if member is None:
        await journal.record(
            session,
            actor=Actor.SYSTEM,
            action="team.login_rejected",
            outcome=Outcome.FAILURE,
            details={"telegram_id": telegram_id, "method": method, "reason": "not_in_team"},
        )
    return member


def member_subject(member: TeamMember) -> Subject:
    return Subject(TEAM_MEMBER_SUBJECT, member.id)
