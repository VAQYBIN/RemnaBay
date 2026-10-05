"""API веб-админки: `/api/admin/…` (решение 0051)."""

from typing import Any

from fastapi import APIRouter, Depends, FastAPI

from remnabay.web.admin import _auth, _brand, _shop, _stats
from remnabay.web.admin._auth import ADMIN_PATH, LOGIN_COOKIE, TELEGRAM_LOGIN_PATH
from remnabay.web.admin._deps import SESSION_COOKIE, same_origin

API_PREFIX = "/api/admin"
# Сюда кнопка login_url передаёт подписанные данные Telegram (1.4)
ADMIN_LOGIN_URL_PATH = API_PREFIX + _auth.router.prefix + TELEGRAM_LOGIN_PATH


def build_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(same_origin)])
    router.include_router(_auth.router)
    router.include_router(_brand.public_router)
    router.include_router(_brand.router)
    router.include_router(_shop.router)
    router.include_router(_stats.router)
    return router


def openapi_schema() -> dict[str, Any]:
    """Схема API админки для генерации клиента (0050, 0051) — без настроек и базы."""
    app = FastAPI(title="RemnaBay Admin API")
    app.include_router(build_router())
    return app.openapi()


__all__ = [
    "ADMIN_LOGIN_URL_PATH",
    "ADMIN_PATH",
    "API_PREFIX",
    "LOGIN_COOKIE",
    "SESSION_COOKIE",
    "build_router",
    "openapi_schema",
]
