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

from remnabay.config import Settings, load_settings
from remnabay.db import create_engine

HEALTH_PATH = "/health"
# Меньше интервала проверки здоровья в Docker, чтобы ответ успевал прийти
HEALTH_DB_TIMEOUT_SECONDS = 3.0

logger = logging.getLogger(__name__)


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


def create_app(settings: Settings) -> FastAPI:
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="RemnaBay", lifespan=lifespan)

    @app.get(
        HEALTH_PATH,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthStatus}},
    )
    async def health(response: Response) -> HealthStatus:  # pyright: ignore[reportUnusedFunction]
        """Процесс жив и база отвечает — 200, иначе 503."""
        try:
            async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS), engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError, OSError, TimeoutError:
            logger.warning("Проверка здоровья: база недоступна", exc_info=True)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthStatus(status="unavailable")
        return HealthStatus(status="ok")

    return app


def create_app_from_env() -> FastAPI:
    """Фабрика для uvicorn: настройки читаются в процессе сервера (в том числе при --reload)."""
    return create_app(load_settings())
