"""Прогоны миграции: запуск, применение, цепочка задач (9.1, 9.3, 9.14; решение 0071).

Прогон идёт небольшими задачами очереди (4.28):
1. `migration.read_panel` читает пользователей панели страницами в снимок прогона —
   только чтение, панель не меняется (9.6);
2. `migration.plan` считает план и отчёт. Пробный прогон на этом заканчивается;
3. `migration.apply` применяет план порциями клиентов, последняя порция — ещё и
   реферальные связи.

Одновременно идёт один прогон. Применить можно только последний пробный прогон своего
вида и не позже суток после него; применение заново читает панель (9.3, 0071).
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.migration_runs import (
    MigrationKind,
    MigrationPanelUser,
    MigrationRun,
    MigrationRunState,
)
from remnabay.domain.tariffs import Tariff
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.messaging import load_texts
from remnabay.migration._apply import Context, apply_client, apply_link, shop_state
from remnabay.migration._format import MigrationFile
from remnabay.migration._plan import MappingRule, MigrationReport, Plan, make_plan
from remnabay.panel import User
from remnabay.queue import TaskContext, attempts_of, task

RUN_SUBJECT = "migration_run"
# Сколько пользователей панели читать за одну задачу
PAGE_SIZE = 500
# Сколько клиентов применять за одну задачу
APPLY_BATCH = 50
# Сколько живёт пробный прогон, который можно применить (0071)
DRY_RUN_TTL = timedelta(days=1)
# Ключ блокировки «один прогон за раз»
_LOCK = 0x6D696772


class RunError(StrEnum):
    BUSY = "busy"
    NOT_FOUND = "not_found"
    NOT_DRY_RUN = "not_dry_run"
    NOT_FINISHED = "not_finished"
    NOT_LATEST = "not_latest"
    EXPIRED = "expired"
    ALREADY_APPLIED = "already_applied"
    UNKNOWN_TARIFF = "unknown_tariff"


class MigrationRunError(Exception):
    def __init__(self, reason: RunError) -> None:
        super().__init__(reason.value)
        self.reason = reason


class RunArgs(BaseModel):
    run_id: int


class StepArgs(BaseModel):
    run_id: int
    start: int = 0


async def _lock(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_LOCK)))


async def running_run(session: AsyncSession) -> MigrationRun | None:
    return await session.scalar(select(MigrationRun).where(MigrationRun.finished_at.is_(None)))


async def _check_mapping(session: AsyncSession, mapping: list[MappingRule]) -> None:
    ids = {rule.tariff_id for rule in mapping}
    found = set(await session.scalars(select(Tariff.id).where(Tariff.id.in_(ids))))
    if ids - found:
        raise MigrationRunError(RunError.UNKNOWN_TARIFF)


def _source(file: MigrationFile) -> dict[str, JsonValue]:
    return {"format_version": file.format_version, **file.source.model_dump(mode="json")}


async def start_dry_run(
    session: AsyncSession,
    *,
    member_id: int,
    mapping: list[MappingRule],
    file: MigrationFile | None = None,
) -> MigrationRun:
    """Пробный прогон усыновления или, с файлом, импорта (9.1, 9.10)."""
    await _lock(session)
    if await running_run(session) is not None:
        raise MigrationRunError(RunError.BUSY)
    await _check_mapping(session, mapping)
    run = MigrationRun(
        kind=MigrationKind.ADOPTION if file is None else MigrationKind.IMPORT,
        dry_run=True,
        state=MigrationRunState.READING_PANEL,
        tariff_mapping=[rule.model_dump(mode="json") for rule in mapping],
        source=_source(file) if file is not None else None,
        file=file.model_dump(mode="json") if file is not None else None,
        started_by_id=member_id,
    )
    session.add(run)
    await session.flush()
    await read_panel.enqueue(session, StepArgs(run_id=run.id))
    return run


async def rerun(
    session: AsyncSession, *, run_id: int, member_id: int, mapping: list[MappingRule]
) -> MigrationRun:
    """Пробный прогон заново — с новым сопоставлением, тем же видом и файлом (9.15)."""
    previous = await session.get(MigrationRun, run_id)
    if previous is None or not previous.dry_run:
        raise MigrationRunError(RunError.NOT_FOUND)
    file = MigrationFile.model_validate(previous.file) if previous.file is not None else None
    return await start_dry_run(session, member_id=member_id, mapping=mapping, file=file)


async def apply_dry_run(
    session: AsyncSession, *, run_id: int, member_id: int, now: datetime
) -> MigrationRun:
    """Применение пробного прогона (9.3): последнего своего вида, не старше суток."""
    await _lock(session)
    dry = await session.get(MigrationRun, run_id)
    if dry is None:
        raise MigrationRunError(RunError.NOT_FOUND)
    if not dry.dry_run:
        raise MigrationRunError(RunError.NOT_DRY_RUN)
    if await running_run(session) is not None:
        raise MigrationRunError(RunError.BUSY)
    if dry.state != MigrationRunState.DONE or dry.finished_at is None:
        raise MigrationRunError(RunError.NOT_FINISHED)
    newer = await session.scalar(
        select(MigrationRun.id).where(
            MigrationRun.kind == dry.kind, MigrationRun.dry_run, MigrationRun.id > dry.id
        )
    )
    if newer is not None:
        raise MigrationRunError(RunError.NOT_LATEST)
    if await session.scalar(select(MigrationRun.id).where(MigrationRun.dry_run_id == dry.id)):
        raise MigrationRunError(RunError.ALREADY_APPLIED)
    if now - dry.finished_at > DRY_RUN_TTL:
        raise MigrationRunError(RunError.EXPIRED)
    run = MigrationRun(
        kind=dry.kind,
        dry_run=False,
        dry_run_id=dry.id,
        state=MigrationRunState.READING_PANEL,
        tariff_mapping=dry.tariff_mapping,
        source=dry.source,
        started_by_id=member_id,
    )
    session.add(run)
    await session.flush()
    await read_panel.enqueue(session, StepArgs(run_id=run.id))
    return run


async def _run_in(
    session: AsyncSession, run_id: int, state: MigrationRunState
) -> MigrationRun | None:
    run = await session.get(MigrationRun, run_id, with_for_update=True)
    return run if run is not None and run.state == state else None


async def _snapshot(session: AsyncSession, run_id: int, ids: list[int] | None = None) -> list[User]:
    query = select(MigrationPanelUser.data).where(MigrationPanelUser.run_id == run_id)
    if ids is not None:
        query = query.where(MigrationPanelUser.panel_user_id.in_(ids))
    return [User.model_validate(data) for data in await session.scalars(query)]


async def _finish(
    session: AsyncSession, run: MigrationRun, report: MigrationReport, now: datetime
) -> None:
    run.report = report.model_dump(mode="json")
    run.plan = None
    run.state = MigrationRunState.DONE
    run.finished_at = now
    await session.execute(delete(MigrationPanelUser).where(MigrationPanelUser.run_id == run.id))
    await record(
        session,
        actor=Actor.team_member(run.started_by_id),
        action="migration.dry_run" if run.dry_run else "migration.applied",
        outcome=Outcome.SUCCESS,
        subject=Subject(RUN_SUBJECT, run.id),
        details={
            "kind": run.kind.value,
            "adopt": report.adopt,
            "clients": report.clients,
            "file_clients": report.file.file_clients if report.file else None,
            "conflicts": report.conflicts,
            "unrecognized": report.unrecognized,
            "skipped": report.skipped,
        },
    )


async def _file_of(session: AsyncSession, run: MigrationRun) -> MigrationFile | None:
    source = run if run.dry_run else await session.get(MigrationRun, run.dry_run_id)
    if source is None or source.file is None:
        return None
    return MigrationFile.model_validate(source.file)


@task("migration.read_panel", StepArgs, needs_attention=False)
async def read_panel(context: TaskContext, args: StepArgs) -> None:
    """Страница пользователей панели в снимок прогона; после последней — расчёт плана."""
    session = context.session
    run = await _run_in(session, args.run_id, MigrationRunState.READING_PANEL)
    if run is None or args.start < run.processed:
        return
    page = await runtime.current().panel.list_users(start=args.start, size=PAGE_SIZE)
    if page.users:
        await session.execute(
            insert(MigrationPanelUser)
            .values(
                [
                    {
                        "run_id": run.id,
                        "panel_user_id": user.id,
                        "data": user.model_dump(mode="json", by_alias=True),
                    }
                    for user in page.users
                ]
            )
            .on_conflict_do_nothing()
        )
    run.processed = args.start + len(page.users)
    run.total = page.total
    if page.users and run.processed < page.total:
        await read_panel.enqueue(session, StepArgs(run_id=run.id, start=run.processed))
        return
    run.state = MigrationRunState.PLANNING
    await plan_run.enqueue(session, RunArgs(run_id=run.id))


@task("migration.plan", RunArgs, needs_attention=False)
async def plan_run(context: TaskContext, args: RunArgs) -> None:
    """План и отчёт по снимку (9.2, 9.11). Пробный прогон на этом заканчивается."""
    session = context.session
    run = await _run_in(session, args.run_id, MigrationRunState.PLANNING)
    if run is None:
        return
    mapping = [MappingRule.model_validate(rule) for rule in run.tariff_mapping]
    plan, report = make_plan(
        await _snapshot(session, run.id),
        await shop_state(session),
        mapping,
        await _file_of(session, run),
    )
    if run.dry_run:
        await _finish(session, run, report, datetime.now(UTC))
        return
    run.plan = plan.model_dump(mode="json")
    run.report = report.model_dump(mode="json")
    run.state = MigrationRunState.APPLYING
    run.processed = 0
    run.total = len(plan.clients)
    await apply_step.enqueue(session, StepArgs(run_id=run.id))


@task("migration.apply", StepArgs, needs_attention=False)
async def apply_step(context: TaskContext, args: StepArgs) -> None:
    """Порция клиентов плана; последняя — ещё и реферальные связи и итоговый отчёт."""
    session = context.session
    run = await _run_in(session, args.run_id, MigrationRunState.APPLYING)
    if run is None or run.plan is None or args.start < run.processed:
        return
    plan = Plan.model_validate(run.plan)
    report = MigrationReport.model_validate(run.report)
    items = plan.clients[args.start : args.start + APPLY_BATCH]
    ids = [planned.panel_user_id for item in items for planned in item.subscriptions]
    users = {user.id: user for user in await _snapshot(session, run.id, ids)}
    now = datetime.now(UTC)
    apply_context = Context(
        run_id=run.id,
        actor=Actor.team_member(run.started_by_id),
        texts=await load_texts(session),
        now=now,
    )
    for item in items:
        applied = await apply_client(session, item, users, apply_context)
        # Итоговый отчёт — фактический: подписку, которую не усыновили, не считаем
        report.skipped += applied.skipped
        report.adopt -= applied.not_adopted
    run.processed = args.start + len(items)
    if run.processed < len(plan.clients):
        run.report = report.model_dump(mode="json")
        await apply_step.enqueue(session, StepArgs(run_id=run.id, start=run.processed))
        return
    for link in plan.links:
        if not await apply_link(session, link, apply_context):
            report.skipped += 1
    await _finish(session, run, report, now)


async def _failed(session: AsyncSession, task_id: int, run_id: int) -> None:
    """Прогон не удался: снимок удаляется, можно запустить новый пробный прогон.
    Уже применённое остаётся: повторное применение его пропустит (9.7)."""
    run = await session.get(MigrationRun, run_id, with_for_update=True)
    if run is None or run.finished_at is not None:
        return
    run.state = MigrationRunState.FAILED
    run.finished_at = datetime.now(UTC)
    run.plan = None
    attempts = await attempts_of(session, task_id)
    run.error = (attempts[-1].error if attempts else None) or "Прогон прервался"
    await session.execute(delete(MigrationPanelUser).where(MigrationPanelUser.run_id == run.id))
    await record(
        session,
        actor=Actor.SYSTEM,
        action="migration.failed",
        outcome=Outcome.FAILURE,
        subject=Subject(RUN_SUBJECT, run.id),
        details={"kind": run.kind.value, "dry_run": run.dry_run},
    )


@read_panel.on_failed
async def _read_panel_failed(session: AsyncSession, task_id: int, args: StepArgs) -> None:
    await _failed(session, task_id, args.run_id)


@plan_run.on_failed
async def _plan_failed(session: AsyncSession, task_id: int, args: RunArgs) -> None:
    await _failed(session, task_id, args.run_id)


@apply_step.on_failed
async def _apply_failed(session: AsyncSession, task_id: int, args: StepArgs) -> None:
    await _failed(session, task_id, args.run_id)


TASKS = (read_panel, plan_run, apply_step)
