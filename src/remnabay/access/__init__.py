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
    login_with_telegram,
    open_bot_login,
    poll_bot_login,
    start_bot_login,
)
from remnabay.access._owner import TEAM_MEMBER_SUBJECT, ensure_owner
from remnabay.access._sessions import create_session, end_session, member_for_session
from remnabay.access._settings import LOGIN_TTL, SESSION_TTL
from remnabay.access._telegram_auth import TelegramAuth, sign_login_url, verify_login_url

__all__ = [
    "LOGIN_TTL",
    "SESSION_TTL",
    "START_PREFIX",
    "TEAM_MEMBER_SUBJECT",
    "LoginClosed",
    "LoginOpened",
    "NewLogin",
    "PollResult",
    "PollStatus",
    "TelegramAuth",
    "active_member",
    "confirm_bot_login",
    "create_session",
    "end_session",
    "ensure_owner",
    "is_login_link",
    "login_with_telegram",
    "member_for_session",
    "open_bot_login",
    "poll_bot_login",
    "sign_login_url",
    "start_bot_login",
    "verify_login_url",
]
