"""Приложение FastAPI роли «веб»."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from remnabay.access import TelegramOidc, ensure_owner
from remnabay.bot import (
    TELEGRAM_WEBHOOK_PATH,
    BotWebhookStatus,
    Polling,
    create_bot,
    create_dispatcher,
    register_webhook,
)
from remnabay.config import Settings, load_settings
from remnabay.crypto import SecretBox
from remnabay.db import create_engine, create_session_factory, describe_error
from remnabay.healthcheck import HEALTH_PATH
from remnabay.panel import PanelClient
from remnabay.payments import Providers
from remnabay.payments.yookassa import YooKassaKind, yookassa_http
from remnabay.shop_settings import TELEGRAM_WEBHOOK_SECRET, generated_secret
from remnabay.web import (
    admin,
    admin_static,
    brand_files,
    panel_webhook,
    payment_webhook,
    redirect_page,
    telegram_webhook,
)
from remnabay.web.admin import LOGIN_PAGE_PATH

# Меньше интервала проверки здоровья в Docker, чтобы ответ успевал прийти
HEALTH_DB_TIMEOUT_SECONDS = 3.0
# Пауза между попытками дождаться базы при запуске без самоконтроля (разработка)
DATABASE_RETRY = timedelta(seconds=15)

logger = logging.getLogger(__name__)


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


@dataclass(frozen=True)
class Watchdog:
    """Самоконтроль веба (0073): база дольше `lost_after` не отвечает — веб потерял
    работоспособность и вызывает `on_lost`, чтобы процесс завершился с ошибкой и
    Docker поднял его заново (`restart: unless-stopped`)."""

    on_lost: Callable[[], None]
    interval: timedelta = timedelta(seconds=15)
    lost_after: timedelta = timedelta(minutes=3)


async def _database_available(engine: AsyncEngine) -> SQLAlchemyError | OSError | None:
    """`None` — база ответила, иначе — ошибка, с которой она не ответила."""
    try:
        async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        return exc
    return None


async def _watch_database(engine: AsyncEngine, watchdog: Watchdog) -> None:
    last_ok = time.monotonic()
    while True:
        error = await _database_available(engine)
        if error is None:
            last_ok = time.monotonic()
        elif time.monotonic() - last_ok > watchdog.lost_after.total_seconds():
            logger.critical(
                "Веб потерял работоспособность и завершается: база не отвечает дольше %.0f с (%s)",
                watchdog.lost_after.total_seconds(),
                describe_error(error),
            )
            watchdog.on_lost()
            return
        await asyncio.sleep(watchdog.interval.total_seconds())


async def _wait_for_database(engine: AsyncEngine, watchdog: Watchdog | None) -> bool:
    """Запуск веба ждёт базу: после самоперезапуска (0073) она может быть ещё недоступна.

    Пока ждём — одна строка в логе на попытку, без трассировки (решение 0076). Если
    база не ответила за `lost_after` самоконтроля, веб завершается, как при потере
    работоспособности, и Docker поднимает его заново. `False` — база так и не ответила.
    """
    interval = (watchdog.interval if watchdog else DATABASE_RETRY).total_seconds()
    started = time.monotonic()
    while (error := await _database_available(engine)) is not None:
        if (
            watchdog is not None
            and time.monotonic() - started > watchdog.lost_after.total_seconds()
        ):
            logger.critical(
                "Веб не запустился и завершается: база не отвечает дольше %.0f с (%s)",
                watchdog.lost_after.total_seconds(),
                describe_error(error),
            )
            watchdog.on_lost()
            return False
        logger.warning(
            "Веб ждёт базу: она не отвечает (%s), повтор через %.0f с",
            describe_error(error),
            interval,
        )
        await asyncio.sleep(interval)
    return True


def create_app(
    settings: Settings, *, startup: bool = False, watchdog: Watchdog | None = None
) -> FastAPI:
    """Приложение веба.

    `startup` — действия при запуске магазина: владелец из `.env` (1.2), вебхук бота
    или опрос при разработке. В тестах они выключены: не меняют общую тестовую базу
    и не обращаются к Telegram.

    `watchdog` — самоконтроль процесса (0073); без него веб только отвечает 503
    на проверку здоровья.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    box = SecretBox(settings.encryption_key.get_secret_value())
    proxy = settings.telegram_proxy_url
    bot = create_bot(settings.bot_token.get_secret_value(), proxy=proxy)
    # Вход в админку через Telegram OpenID Connect (0053)
    oidc = TelegramOidc(
        settings.bot_id, settings.telegram_login_client_secret.get_secret_value(), proxy=proxy
    )
    # Чек-лист проверяет панель из веба (1.8, 1.10)
    panel = PanelClient(str(settings.panel_url), settings.panel_token.get_secret_value())
    # Платёжные провайдеры с ключами оператора из админки (3.27)
    yookassa = yookassa_http()
    providers = Providers(box, [YooKassaKind(yookassa)])
    dispatcher = create_dispatcher(
        sessions,
        admin_login_url=settings.public_link(LOGIN_PAGE_PATH),
        providers=providers,
        panel=panel,
        shop_url=settings.public_link(""),
    )
    # Пункт «Вебхук бота» в чек-листе
    bot_webhook = BotWebhookStatus()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        polling: Polling | None = None
        registering: asyncio.Task[None] | None = None
        ready = not startup or await _wait_for_database(engine, watchdog)
        if startup and ready:
            async with sessions() as session:
                await ensure_owner(session, settings.owner_telegram_id)
                secret = await generated_secret(session, box, TELEGRAM_WEBHOOK_SECRET)
                await session.commit()
            if settings.dev_mode:
                polling = Polling(bot, dispatcher)
                await polling.start()
                bot_webhook.polling = True
            else:
                url = settings.public_link(TELEGRAM_WEBHOOK_PATH)
                registering = asyncio.create_task(
                    register_webhook(bot, dispatcher, url, secret, bot_webhook)
                )
        watching = (
            asyncio.create_task(_watch_database(engine, watchdog)) if watchdog and ready else None
        )
        yield
        for task in (watching, registering):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if polling is not None:
            await polling.stop()
        await bot.session.close()
        await panel.aclose()
        await yookassa.aclose()
        await oidc.aclose()
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
    app.state.bot_webhook = bot_webhook
    app.state.panel = panel
    app.state.oidc = oidc
    app.state.providers = providers
    app.include_router(panel_webhook.router)
    app.include_router(payment_webhook.router)
    app.include_router(telegram_webhook.router)
    app.include_router(admin.build_router())
    app.include_router(admin_static.build_router())
    app.include_router(brand_files.router)
    app.include_router(redirect_page.router)

    @app.get(
        HEALTH_PATH,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthStatus}},
    )
    async def health(response: Response) -> HealthStatus:  # pyright: ignore[reportUnusedFunction]
        """Процесс жив и база отвечает — 200, иначе 503."""
        error = await _database_available(engine)
        if error is not None:
            # Одна строка без трассировки: проверка идёт каждые 15 секунд,
            # и при недоступной базе трассировки заполнили бы весь лог
            logger.warning("Проверка здоровья: база недоступна (%s)", describe_error(error))
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
