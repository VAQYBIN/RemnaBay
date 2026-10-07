"""Вход в админку и выход (1.4–1.6, решения 0051, 0053)."""

import hmac
import logging
from datetime import UTC, datetime
from typing import Literal

from aiogram import Bot
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from remnabay.access import (
    LOGIN_TTL,
    VIA_BOT,
    VIA_OIDC,
    LoginClosed,
    OidcAttempt,
    OidcError,
    PollStatus,
    TelegramOidc,
    create_session,
    end_session,
    member_for_login,
    member_for_session,
    poll_bot_login,
    start_bot_login,
)
from remnabay.config import Settings
from remnabay.crypto import SecretBox
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.shop_settings import get_setting
from remnabay.web.admin._deps import SESSION_COOKIE, AppSettings, DbSession, Member

ADMIN_PATH = "/admin/"
# Страница входа админки: на неё ведёт кнопка бота на /admin (0053)
LOGIN_PAGE_PATH = "/admin/login"
LOGIN_COOKIE = "remnabay_login"
OIDC_COOKIE = "remnabay_oidc"
OIDC_START_PATH = "/telegram/start"
OIDC_CALLBACK_PATH = "/telegram/callback"
OIDC_COOKIE_PATH = "/api/admin/auth/telegram"
# Адрес возврата от Telegram — его оператор вносит в @BotFather (Redirect URI)
OIDC_CALLBACK_URL = OIDC_COOKIE_PATH + "/callback"

logger = logging.getLogger(__name__)
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
        token, expires_at = await create_session(session, result.member, VIA_BOT, now=now)
        _set_cookie(response, settings, SESSION_COOKIE, token, expires_at)
    await session.commit()
    if result.status != PollStatus.PENDING:
        response.delete_cookie(LOGIN_COOKIE, path="/")
    return LoginPollOut(status=result.status.value)


def _login_error(reason: str) -> RedirectResponse:
    return RedirectResponse(f"{ADMIN_PATH}login?error={reason}", status.HTTP_303_SEE_OTHER)


@router.get(OIDC_START_PATH, include_in_schema=False)
async def telegram_start(
    request: Request, session: DbSession, settings: AppSettings
) -> RedirectResponse:
    """«Войти через Telegram»: на страницу Telegram с новой попыткой входа (1.4, 0053)."""
    oidc: TelegramOidc = request.app.state.oidc
    box: SecretBox = request.app.state.box
    attempt = OidcAttempt.new()
    ttl = await get_setting(session, LOGIN_TTL)
    redirect = RedirectResponse(
        oidc.authorize_url(attempt, settings.public_link(OIDC_CALLBACK_URL)),
        status.HTTP_303_SEE_OTHER,
    )
    # Lax: возврат со страницы Telegram — межсайтовый переход, cookie должна дойти
    redirect.set_cookie(
        OIDC_COOKIE,
        attempt.seal(box),
        max_age=int(ttl.total_seconds()),
        path=OIDC_COOKIE_PATH,
        secure=not settings.dev_mode,
        httponly=True,
        samesite="lax",
    )
    return redirect


@router.get(OIDC_CALLBACK_PATH, include_in_schema=False)
async def telegram_callback(
    request: Request, session: DbSession, settings: AppSettings
) -> RedirectResponse:
    """Сюда Telegram возвращает участника с кодом. Попытка одноразовая и действует
    срок подтверждения входа (1.5); чужой аккаунт — отказ (1.6)."""
    oidc: TelegramOidc = request.app.state.oidc
    box: SecretBox = request.app.state.box
    now = datetime.now(UTC)
    sealed = request.cookies.get(OIDC_COOKIE)
    ttl = await get_setting(session, LOGIN_TTL)
    attempt = OidcAttempt.unseal(box, sealed, max_age=ttl) if sealed else None
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")
    if (
        attempt is None
        or not code
        or not hmac.compare_digest(state.encode(), attempt.state.encode())
    ):
        # Повторный заход на адрес возврата уже после входа — просто в админку
        token = request.cookies.get(SESSION_COOKIE)
        if token and await member_for_session(session, token, now=now) is not None:
            response = RedirectResponse(ADMIN_PATH, status.HTTP_303_SEE_OTHER)
        else:
            response = _login_error(LoginClosed.EXPIRED.value)
        response.delete_cookie(OIDC_COOKIE, path=OIDC_COOKIE_PATH)
        return response
    try:
        telegram_id = await oidc.telegram_id(
            code, attempt, settings.public_link(OIDC_CALLBACK_URL), now=now
        )
    except OidcError as error:
        logger.warning("Вход через Telegram не подтверждён: %s", error)
        response = _login_error("failed")
        response.delete_cookie(OIDC_COOKIE, path=OIDC_COOKIE_PATH)
        return response
    member = await member_for_login(session, telegram_id, VIA_OIDC)
    if member is None:
        await session.commit()
        response = _login_error(LoginClosed.REJECTED.value)
    else:
        token, expires_at = await create_session(session, member, VIA_OIDC, now=now)
        await session.commit()
        response = RedirectResponse(ADMIN_PATH, status.HTTP_303_SEE_OTHER)
        _set_cookie(response, settings, SESSION_COOKIE, token, expires_at)
    response.delete_cookie(OIDC_COOKIE, path=OIDC_COOKIE_PATH)
    return response


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
