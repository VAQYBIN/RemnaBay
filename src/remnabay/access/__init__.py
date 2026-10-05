"""Доступ команды к админке: владелец из `.env`, вход через Telegram, сессии (блок 1)."""

from remnabay.access._owner import TEAM_MEMBER_SUBJECT, ensure_owner

__all__ = ["TEAM_MEMBER_SUBJECT", "ensure_owner"]
