"""Миграции Alembic применяются к чистой базе, схема совпадает с моделями."""

import pytest
from alembic import command

from remnabay.config import load_settings
from remnabay.migrations import alembic_config, upgrade_to_head


@pytest.mark.usefixtures("valid_env")
def test_upgrade_to_head_and_schema_matches_models() -> None:
    """`remnabay migrate` проходит, и после него нет изменений моделей без миграции."""
    settings = load_settings(env_file=None)

    upgrade_to_head(settings)

    command.check(alembic_config(settings.sqlalchemy_database_url))


@pytest.mark.usefixtures("valid_env")
def test_downgrade_to_base_and_upgrade_again() -> None:
    """Каждую миграцию можно откатить: откат до пустой базы и повторное применение проходят."""
    settings = load_settings(env_file=None)
    config = alembic_config(settings.sqlalchemy_database_url)

    upgrade_to_head(settings)
    command.downgrade(config, "base")
    upgrade_to_head(settings)

    command.check(config)
