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
from remnabay.bot import (
    TELEGRAM_WEBHOOK_PATH,
    Polling,
    create_bot,
    create_dispatcher,
    register_webhook,
)
from remnabay.config import Settings, load_settings
from remnabay.crypto import SecretBox
from remnabay.db import create_engine, create_session_factory
from remnabay.shop_settings import TELEGRAM_WEBHOOK_SECRET, generated_secret
from remnabay.web import admin, admin_static, panel_webhook, telegram_webhook
from remnabay.web.admin import ADMIN_LOGIN_URL_PATH

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
    """Приложение веба.

    `startup` — действия при запуске магазина: владелец из `.env` (1.2), вебхук бота
    или опрос при разработке. В тестах они выключены: не меняют общую тестовую базу
    и не обращаются к Telegram.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    box = SecretBox(settings.encryption_key.get_secret_value())
    bot = create_bot(settings.bot_token.get_secret_value())
    dispatcher = create_dispatcher(
        sessions, admin_login_url=settings.public_link(ADMIN_LOGIN_URL_PATH)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        polling: Polling | None = None
        if startup:
            async with sessions() as session:
                await ensure_owner(session, settings.owner_telegram_id)
                secret = await generated_secret(session, box, TELEGRAM_WEBHOOK_SECRET)
                await session.commit()
            if settings.dev_mode:
                polling = Polling(bot, dispatcher)
                await polling.start()
            else:
                url = settings.public_link(TELEGRAM_WEBHOOK_PATH)
                await register_webhook(bot, dispatcher, url, secret)
        yield
        if polling is not None:
            await polling.stop()
        await bot.session.close()
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
    app.state.box = box
    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.include_router(panel_webhook.router)
    app.include_router(telegram_webhook.router)
    app.include_router(admin.build_router())
    app.include_router(admin_static.build_router())

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
    # При --reload сервер работает в дочернем процессе, где лог ещё не настроен
    from remnabay.cli import configure_logging

    configure_logging()
    return create_app(load_settings(), startup=True)
