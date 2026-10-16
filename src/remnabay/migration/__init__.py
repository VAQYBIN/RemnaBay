"""Миграция: усыновление пользователей панели и импорт промежуточного формата (блок 9).

Остальной код знает только этот интерфейс: прочитать файл (`parse_file`), запустить
пробный прогон (`start_dry_run`, `rerun`), применить его (`apply_dry_run`), узнать, идёт
ли прогон (`running_run`). Отчёт — `MigrationReport`; задачи прогона — `TASKS`.
"""

from remnabay.migration._format import (
    SUPPORTED_VERSIONS,
    FileClient,
    FormatError,
    MigrationFile,
    PanelUserRef,
    Source,
    parse_file,
)
from remnabay.migration._plan import (
    GroupReport,
    ImportReport,
    MappingRule,
    MigrationReport,
    Problem,
    ProblemReason,
    Squad,
)
from remnabay.migration._runs import (
    DRY_RUN_TTL,
    RUN_SUBJECT,
    TASKS,
    MigrationRunError,
    RunError,
    apply_dry_run,
    rerun,
    running_run,
    start_dry_run,
)

__all__ = [
    "DRY_RUN_TTL",
    "RUN_SUBJECT",
    "SUPPORTED_VERSIONS",
    "TASKS",
    "FileClient",
    "FormatError",
    "GroupReport",
    "ImportReport",
    "MappingRule",
    "MigrationFile",
    "MigrationReport",
    "MigrationRunError",
    "PanelUserRef",
    "Problem",
    "ProblemReason",
    "RunError",
    "Source",
    "Squad",
    "apply_dry_run",
    "parse_file",
    "rerun",
    "running_run",
    "start_dry_run",
]
