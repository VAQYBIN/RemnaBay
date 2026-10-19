"""Команда `remnabay`: один образ, роль задаётся командой запуска (0026)."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from remnabay import healthcheck
from remnabay.config import ConfigError, Settings, load_settings
from remnabay.crypto import generate_key

# Код выхода при неверных параметрах .env (1.1)
EXIT_CONFIG_ERROR = 2
# Код выхода процесса, который потерял работоспособность (0073): Docker поднимет его заново
EXIT_UNHEALTHY = 1
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Библиотеки, которые пишут строку на каждый запрос (клиент панели — раз в минуту
# только от проверки связи): в логе — только их предупреждения и ошибки
QUIET_LOGGERS = ("httpx2",)

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="remnabay", description="Магазин VPN для Remnawave")
    roles = parser.add_subparsers(dest="command", required=True)

    web = roles.add_parser("web", help="веб: админка, API, вебхуки, проверка здоровья")
    # В контейнере сервер слушает все интерфейсы; наружу его открывает обратный прокси
    web.add_argument("--host", default="0.0.0.0")  # noqa: S104
    web.add_argument("--port", type=int, default=healthcheck.WEB_PORT)
    web.add_argument("--reload", action="store_true", help="перезапуск при изменении кода")

    roles.add_parser("worker", help="воркер: фоновые и периодические задачи")
    roles.add_parser("migrate", help="применить миграции базы")

    check = roles.add_parser("healthcheck", help="проверка здоровья для Docker")
    check.add_argument("role", choices=["web", "worker"])

    roles.add_parser("generate-key", help="создать ключ шифрования для ENCRYPTION_KEY в .env")
    roles.add_parser("openapi", help="вывести схему API админки (для клиента админки)")
    roles.add_parser("default-brand-css", help="вывести токены бренда RemnaBay для админки")

    adapter = roles.add_parser(
        "adapter", help="файл миграции из базы старого бота (только чтение базы)"
    )
    adapter.add_argument("source_bot", choices=["remnawave-tg-shop"], help="какой бот")
    adapter.add_argument("--source", required=True, help="адрес базы старого бота: postgresql://…")
    adapter.add_argument("--output", required=True, help="куда записать файл JSON")

    return parser.parse_args(argv)


def _adapter(source: str, output: str) -> int:
    import psycopg

    from remnabay.migration.adapters import remnawave_tg_shop

    try:
        totals = remnawave_tg_shop.run(source, output)
    except (psycopg.Error, ValueError) as exc:
        print(f"Не удалось прочитать базу старого бота: {exc}", file=sys.stderr)
        return 1
    print(f"Файл миграции: {output}")
    for name, value in totals.items():
        print(f"  {name}: {value}")
    return 0


def _serve_web(settings: Settings, host: str, port: int) -> int:
    """Рабочий режим веба: сервер останавливается и сам, если потерял работоспособность."""
    import uvicorn

    from remnabay.web.app import Watchdog, create_app

    server: uvicorn.Server | None = None
    lost = False

    def stop_unhealthy() -> None:
        nonlocal lost
        lost = True
        if server is not None:
            server.should_exit = True

    app = create_app(settings, startup=True, watchdog=Watchdog(on_lost=stop_unhealthy))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    server.run()
    return EXIT_UNHEALTHY if lost else 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)

    if args.command == "healthcheck":
        healthy = healthcheck.check_web() if args.role == "web" else healthcheck.check_worker()
        return 0 if healthy else 1

    if args.command == "generate-key":
        # Ключ нужен до заполнения .env, поэтому команда не читает настройки
        print(generate_key())
        return 0

    if args.command == "openapi":
        # Схема нужна при сборке, без .env и базы (0049)
        from remnabay.web.admin import openapi_schema

        print(json.dumps(openapi_schema(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "default-brand-css":
        from remnabay.brand import default_brand_css

        print(default_brand_css(), end="")
        return 0

    if args.command == "adapter":
        # Адаптер запускает оператор при переезде: настройки магазина ему не нужны (9.9)
        return _adapter(args.source, args.output)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc.render(), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.command == "web":
        if not args.reload:
            return _serve_web(settings, args.host, args.port)
        import uvicorn

        # Разработка: сервер перезапускается при изменении кода и сам читает настройки
        uvicorn.run(
            "remnabay.web.app:create_app_from_env",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            # Следить только за кодом пакета, а не за всем окружением
            reload_dirs=[str(Path(__file__).parent)],
        )
    elif args.command == "worker":
        from remnabay.worker.main import run

        if not run(settings):
            return EXIT_UNHEALTHY
    else:
        from remnabay.migrations import upgrade_to_head

        upgrade_to_head(settings)
        logger.info("Миграции применены")
    return 0
