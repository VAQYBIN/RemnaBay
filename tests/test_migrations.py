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
