"""Вход в админку и выход (1.4–1.6, решение 0051)."""

from datetime import UTC, datetime
from typing import Literal

from aiogram import Bot
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from remnabay.access import (
    LoginClosed,
    PollStatus,
    create_session,
    end_session,
    login_with_telegram,
    poll_bot_login,
    start_bot_login,
    verify_login_url,
)
from remnabay.config import Settings
from remnabay.domain.team import LoginMethod, TeamMember, TeamRole
from remnabay.web.admin._deps import SESSION_COOKIE, AppSettings, DbSession, Member

ADMIN_PATH = "/admin/"
TELEGRAM_LOGIN_PATH = "/telegram"
LOGIN_COOKIE = "remnabay_login"

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_cookie(
    response: Response, settings: Settings, name: str, value: str, expires_at: datetime
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max(int((expires_at - datetime.now(UTC)).total_seconds()), 0),
        path="/",
        secure=not settings.dev_mode,
        httponly=True,
        samesite="lax",
    )


class LoginRequestOut(BaseModel):
    """Запрос входа: ссылка на бот и код, который бот покажет в подтверждении."""

    bot_link: str
    code: str
    expires_at: datetime


@router.post("/login-requests")
async def create_login_request(
    request: Request, response: Response, session: DbSession, settings: AppSettings
) -> LoginRequestOut:
    bot: Bot = request.app.state.bot
    new = await start_bot_login(session, now=datetime.now(UTC))
    await session.commit()
    me = await bot.me()
    _set_cookie(response, settings, LOGIN_COOKIE, new.poll_key, new.expires_at)
    return LoginRequestOut(
        bot_link=f"https://t.me/{me.username}?start={new.start_parameter}",
        code=new.code,
        expires_at=new.expires_at,
    )


class LoginPollOut(BaseModel):
    status: Literal["pending", "rejected", "expired", "signed_in"]


@router.post("/login-requests/poll")
async def poll_login_request(
    request: Request, response: Response, session: DbSession, settings: AppSettings
) -> LoginPollOut:
    """Подтверждён ли вход в боте. Подтверждённый — сессия в cookie, один раз (1.5)."""
    poll_key = request.cookies.get(LOGIN_COOKIE)
    if not poll_key:
        return LoginPollOut(status="expired")
    now = datetime.now(UTC)
    result = await poll_bot_login(session, poll_key, now=now)
    if result.status == PollStatus.SIGNED_IN and result.member is not None:
        token, expires_at = await create_session(
            session, result.member, LoginMethod.BOT_CONFIRM, now=now
        )
        _set_cookie(response, settings, SESSION_COOKIE, token, expires_at)
    await session.commit()
    if result.status != PollStatus.PENDING:
        response.delete_cookie(LOGIN_COOKIE, path="/")
    return LoginPollOut(status=result.status.value)


@router.get(TELEGRAM_LOGIN_PATH, include_in_schema=False)
async def telegram_login(
    request: Request, session: DbSession, settings: AppSettings
) -> RedirectResponse:
    """Сюда ведёт кнопка `login_url` из ответа бота на /admin (1.4)."""
    auth = verify_login_url(dict(request.query_params), settings.bot_token.get_secret_value())
    if auth is None:
        return RedirectResponse(f"{ADMIN_PATH}login?error=invalid", status.HTTP_303_SEE_OTHER)
    now = datetime.now(UTC)
    outcome = await login_with_telegram(session, auth, now=now)
    if isinstance(outcome, LoginClosed):
        await session.commit()
        return RedirectResponse(
            f"{ADMIN_PATH}login?error={outcome.value}", status.HTTP_303_SEE_OTHER
        )
    token, expires_at = await create_session(session, outcome, LoginMethod.LOGIN_URL, now=now)
    await session.commit()
    redirect = RedirectResponse(ADMIN_PATH, status.HTTP_303_SEE_OTHER)
    _set_cookie(redirect, settings, SESSION_COOKIE, token, expires_at)
    return redirect


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: DbSession) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await end_session(session, token, now=datetime.now(UTC))
        await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


class MemberOut(BaseModel):
    id: int
    telegram_id: int
    name: str | None
    role: TeamRole


@router.get("/me")
async def me(member: Member) -> MemberOut:
    return _member_out(member)


def _member_out(member: TeamMember) -> MemberOut:
    return MemberOut(
        id=member.id, telegram_id=member.telegram_id, name=member.name, role=member.role
    )
