"""Доступ команды к админке: владелец из `.env`, вход через Telegram, сессии (блок 1)."""

from remnabay.access._login import (
    START_PREFIX,
    LoginClosed,
    LoginOpened,
    NewLogin,
    PollResult,
    PollStatus,
    active_member,
    confirm_bot_login,
    is_login_link,
    member_for_login,
    open_bot_login,
    poll_bot_login,
    start_bot_login,
)
from remnabay.access._oidc import OidcAttempt, OidcError, TelegramOidc
from remnabay.access._owner import TEAM_MEMBER_SUBJECT, ensure_owner
from remnabay.access._sessions import create_session, end_session, member_for_session
from remnabay.access._settings import LOGIN_TTL, SESSION_TTL

# Чем вошёл участник — в записи журнала о входе
VIA_OIDC = "oidc"
VIA_BOT = "bot_confirm"

__all__ = [
    "LOGIN_TTL",
    "SESSION_TTL",
    "START_PREFIX",
    "TEAM_MEMBER_SUBJECT",
    "VIA_BOT",
    "VIA_OIDC",
    "LoginClosed",
    "LoginOpened",
    "NewLogin",
    "OidcAttempt",
    "OidcError",
    "PollResult",
    "PollStatus",
    "TelegramOidc",
    "active_member",
    "confirm_bot_login",
    "create_session",
    "end_session",
    "ensure_owner",
    "is_login_link",
    "member_for_login",
    "member_for_session",
    "open_bot_login",
    "poll_bot_login",
    "start_bot_login",
]
