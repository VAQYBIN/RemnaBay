"""Прогон миграции: усыновление или импорт (блок 9)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import CreatedAt
from remnabay.journal import JsonValue


class MigrationKind(StrEnum):
    ADOPTION = "adoption"
    IMPORT = "import"


class MigrationRunState(StrEnum):
    """Ход прогона: снимок панели, отчёт, применение — цепочкой задач (4.28)."""

    READING_PANEL = "reading_panel"
    PLANNING = "planning"
    APPLYING = "applying"
    DONE = "done"
    FAILED = "failed"


class MigrationRun(Base):
    """Пробный или настоящий прогон: вид, сопоставление тарифов, отчёт, кто и когда."""

    __tablename__ = "migration_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[MigrationKind] = mapped_column(str_enum_type(MigrationKind, "migration_kind"))
    # Сначала всегда пробный прогон (9.1, 9.3)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    # Настоящий прогон — применение этого пробного (9.3); один пробный применяется один раз
    dry_run_id: Mapped[int | None] = mapped_column(ForeignKey("migration_runs.id"), unique=True)
    state: Mapped[MigrationRunState] = mapped_column(
        str_enum_type(MigrationRunState, "migration_run_state"),
        server_default=MigrationRunState.READING_PANEL.value,
    )
    # Сопоставление «сквады и лимит устройств → тариф» (9.15)
    tariff_mapping: Mapped[list[JsonValue]] = mapped_column(JSONB, server_default="[]")
    # Источник импорта: `source` и `format_version` файла (migration-format.md)
    source: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    # Проверенный файл импорта; применение берёт его у своего пробного прогона
    file: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    # Что применить: план, посчитанный по свежему снимку панели; после применения — пусто
    plan: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    report: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    # Ход: сколько из скольких прочитано или применено
    processed: Mapped[int] = mapped_column(Integer, server_default="0")
    total: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    started_at: Mapped[CreatedAt]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MigrationPanelUser(Base):
    """Пользователь панели в снимке прогона. Снимок читается страницами (4.28) и
    удаляется, когда прогон закончен."""

    __tablename__ = "migration_panel_users"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("migration_runs.id", ondelete="CASCADE"), primary_key=True
    )
    panel_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Ответ панели как есть: из него строится подписка при применении
    data: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
