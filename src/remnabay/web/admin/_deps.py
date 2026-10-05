"""Зависимости API админки: участник по сессии, права ролей, защита от чужих сайтов."""

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access import member_for_session
from remnabay.config import Settings
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.web.deps import get_session

SESSION_COOKIE = "remnabay_session"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEV_HOSTS = frozenset({"localhost", "127.0.0.1"})


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def same_origin(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """Изменяющие запросы — только со страниц самого магазина (0051).

    Cookie сессии с `SameSite=Lax` и так не уходит с чужих сайтов в POST-запросах;
    проверка `Origin` закрывает и старые браузеры. При разработке разрешён localhost
    (сервер Vite).
    """
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        if request.headers.get("sec-fetch-site") == "same-origin":
            return
    else:
        own = origin == _origin(str(settings.public_url))
        dev = settings.dev_mode and urlsplit(origin).hostname in _DEV_HOSTS
        if own or dev:
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Запрос не со страницы магазина")


async def current_member(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> TeamMember:
    """Участник действующей сессии, иначе 401."""
    token = request.cookies.get(SESSION_COOKIE)
    member = None
    if token:
        member = await member_for_session(session, token, now=datetime.now(UTC))
    if member is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужно войти")
    return member


async def current_owner(member: Annotated[TeamMember, Depends(current_member)]) -> TeamMember:
    """Только владелец: настройки, деньги, открытие магазина (01-domain, таблица ролей)."""
    if member.role != TeamRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Действие доступно только владельцу")
    return member


type Member = Annotated[TeamMember, Depends(current_member)]
type Owner = Annotated[TeamMember, Depends(current_owner)]
type DbSession = Annotated[AsyncSession, Depends(get_session)]
type AppSettings = Annotated[Settings, Depends(get_settings)]
