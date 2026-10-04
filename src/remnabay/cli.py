"""Команда `remnabay`: один образ, роль задаётся командой запуска (0026)."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from remnabay import healthcheck
from remnabay.config import ConfigError, load_settings

# Код выхода при неверных параметрах .env (1.1)
EXIT_CONFIG_ERROR = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


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

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)

    if args.command == "healthcheck":
        healthy = healthcheck.check_web() if args.role == "web" else healthcheck.check_worker()
        return 0 if healthy else 1

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc.render(), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.command == "web":
        import uvicorn

        uvicorn.run(
            "remnabay.web.app:create_app_from_env",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            # Следить только за кодом пакета, а не за всем окружением
            reload_dirs=[str(Path(__file__).parent)] if args.reload else None,
        )
    elif args.command == "worker":
        from remnabay.worker.main import run

        run()
    else:
        from remnabay.migrations import upgrade_to_head

        upgrade_to_head(settings)
        logger.info("Миграции применены")
    return 0
