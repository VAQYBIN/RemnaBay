"""Прогон миграции: усыновление или импорт (блок 9)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import CreatedAt
from remnabay.journal import JsonValue


class MigrationKind(StrEnum):
    ADOPTION = "adoption"
    IMPORT = "import"


class MigrationRun(Base):
    """Пробный или настоящий прогон: вид, сопоставление тарифов, отчёт, кто и когда."""

    __tablename__ = "migration_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[MigrationKind] = mapped_column(str_enum_type(MigrationKind, "migration_kind"))
    # Сначала всегда пробный прогон (9.1, 9.3)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    # Сопоставление «сквады и лимит устройств → тариф» (9.15)
    tariff_mapping: Mapped[list[JsonValue]] = mapped_column(JSONB, server_default="[]")
    # Источник импорта: `source` и `format_version` файла (migration-format.md)
    source: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    report: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    started_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    started_at: Mapped[CreatedAt]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
