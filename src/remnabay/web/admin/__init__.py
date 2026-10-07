"""API веб-админки: `/api/admin/…` (решение 0051)."""

from typing import Any

from fastapi import APIRouter, Depends, FastAPI

from remnabay.web.admin import (
    _about,
    _auth,
    _brand,
    _payment_settings,
    _payments,
    _shop,
    _stats,
    _tariffs,
)
from remnabay.web.admin._auth import (
    ADMIN_PATH,
    LOGIN_COOKIE,
    LOGIN_PAGE_PATH,
    OIDC_CALLBACK_URL,
    OIDC_COOKIE,
)
from remnabay.web.admin._deps import SESSION_COOKIE, same_origin

API_PREFIX = "/api/admin"


def build_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(same_origin)])
    router.include_router(_auth.router)
    router.include_router(_brand.public_router)
    router.include_router(_brand.router)
    router.include_router(_shop.router)
    router.include_router(_stats.router)
    router.include_router(_tariffs.router)
    router.include_router(_payment_settings.router)
    router.include_router(_payments.router)
    router.include_router(_about.router)
    return router


def openapi_schema() -> dict[str, Any]:
    """Схема API админки для генерации клиента (0050, 0051) — без настроек и базы."""
    app = FastAPI(title="RemnaBay Admin API")
    app.include_router(build_router())
    return app.openapi()


__all__ = [
    "ADMIN_PATH",
    "API_PREFIX",
    "LOGIN_COOKIE",
    "LOGIN_PAGE_PATH",
    "OIDC_CALLBACK_URL",
    "OIDC_COOKIE",
    "SESSION_COOKIE",
    "build_router",
    "openapi_schema",
]
