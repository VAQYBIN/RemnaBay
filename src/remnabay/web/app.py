"""Приложение FastAPI роли «веб»."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from remnabay.access import ensure_owner
from remnabay.config import Settings, load_settings
from remnabay.crypto import SecretBox
from remnabay.db import create_engine, create_session_factory
from remnabay.web import panel_webhook

HEALTH_PATH = "/health"
# Меньше интервала проверки здоровья в Docker, чтобы ответ успевал прийти
HEALTH_DB_TIMEOUT_SECONDS = 3.0

logger = logging.getLogger(__name__)


def _describe_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


def create_app(settings: Settings, *, startup: bool = False) -> FastAPI:
    """Приложение веба. `startup` — действия при запуске магазина (владелец из `.env`);
    в тестах они выключены, чтобы не менять общую тестовую базу."""
    engine = create_engine(settings)
    sessions = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if startup:
            async with sessions() as session:
                await ensure_owner(session, settings.owner_telegram_id)
                await session.commit()
        yield
        await engine.dispose()

    # Документация API — только при разработке (0049)
    docs = settings.dev_mode
    app = FastAPI(
        title="RemnaBay",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.box = SecretBox(settings.encryption_key.get_secret_value())
    app.include_router(panel_webhook.router)

    @app.get(
        HEALTH_PATH,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthStatus}},
    )
    async def health(response: Response) -> HealthStatus:  # pyright: ignore[reportUnusedFunction]
        """Процесс жив и база отвечает — 200, иначе 503."""
        try:
            async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS), engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            # Одна строка без трассировки: проверка идёт каждые 15 секунд,
            # и при недоступной базе трассировки заполнили бы весь лог
            logger.warning("Проверка здоровья: база недоступна (%s)", _describe_error(exc))
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthStatus(status="unavailable")
        return HealthStatus(status="ok")

    return app


def create_app_from_env() -> FastAPI:
    """Фабрика для uvicorn: настройки читаются в процессе сервера (в том числе при --reload)."""
    return create_app(load_settings(), startup=True)
