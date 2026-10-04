"""Миграции схемы базы (Alembic). Применённую миграцию не редактируют — пишут новую."""

from alembic import command
from alembic.config import Config

from remnabay.config import Settings

SCRIPT_LOCATION = "remnabay:migrations"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.attributes["database_url"] = database_url
    return config


def upgrade_to_head(settings: Settings) -> None:
    """Применяет все миграции. Отдельный шаг перед стартом веба и воркера (0026)."""
    command.upgrade(alembic_config(settings.sqlalchemy_database_url), "head")
