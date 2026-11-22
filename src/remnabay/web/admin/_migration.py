"""Миграция (блок 9, экран А9). Только владелец (01-domain, «Миграция и импорт»)."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.migration_runs import MigrationKind, MigrationRun, MigrationRunState
from remnabay.domain.team import TeamMember
from remnabay.journal import JsonValue
from remnabay.migration import (
    DRY_RUN_TTL,
    FormatError,
    MappingRule,
    MigrationReport,
    MigrationRunError,
    RunError,
    apply_dry_run,
    parse_file,
    rerun,
    start_dry_run,
)
from remnabay.shop_settings import shop_time_zone
from remnabay.web._body import read_limited
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(prefix="/migration", tags=["migration"])

# Файл миграции магазина на десятки тысяч клиентов — единицы мегабайт
MAX_FILE_BYTES = 50 * 1024 * 1024
HISTORY_LIMIT = 50

_MESSAGES = {
    RunError.BUSY: "Уже идёт прогон миграции — дождитесь его окончания",
    RunError.NOT_FOUND: "Прогон не найден",
    RunError.NOT_DRY_RUN: "Применить можно только пробный прогон",
    RunError.NOT_FINISHED: "Пробный прогон ещё не закончен или не удался",
    RunError.NOT_LATEST: "Есть более новый пробный прогон — применить можно только его",
    RunError.EXPIRED: "Пробный прогон старше суток — запустите его заново",
    RunError.ALREADY_APPLIED: "Этот пробный прогон уже применён",
    RunError.UNKNOWN_TARIFF: "В сопоставлении есть тариф, которого нет",
}
_STATUSES = {
    RunError.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RunError.UNKNOWN_TARIFF: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class MappingIn(BaseModel):
    mapping: list[MappingRule] = Field(default_factory=list[MappingRule])


class RunOut(BaseModel):
    id: int
    kind: MigrationKind
    dry_run: bool
    state: MigrationRunState
    # Применение — какого пробного прогона; у пробного — его применение, если было
    dry_run_id: int | None
    applied_by_run_id: int | None
    mapping: list[MappingRule]
    source: dict[str, JsonValue] | None
    report: MigrationReport | None
    processed: int
    total: int | None
    error: str | None
    started_by_telegram_id: int | None
    started_at: datetime
    finished_at: datetime | None
    # Можно применить: последний пробный прогон своего вида, не старше суток (9.3)
    can_apply: bool
    apply_until: datetime | None


class RunsOut(BaseModel):
    runs: list[RunOut]
    time_zone: str


def _error(error: MigrationRunError) -> HTTPException:
    code = _STATUSES.get(error.reason, status.HTTP_409_CONFLICT)
    return HTTPException(code, {"reason": error.reason.value, "message": _MESSAGES[error.reason]})


async def _latest_dry_runs(session: AsyncSession) -> set[int]:
    ids: set[int] = set()
    for kind in MigrationKind:
        latest = await session.scalar(
            select(MigrationRun.id)
            .where(MigrationRun.kind == kind, MigrationRun.dry_run)
            .order_by(MigrationRun.id.desc())
            .limit(1)
        )
        if latest is not None:
            ids.add(latest)
    return ids


async def _out(
    session: AsyncSession,
    run: MigrationRun,
    *,
    latest: set[int],
    applied: dict[int, int],
    running: bool,
    now: datetime,
) -> RunOut:
    member = await session.get(TeamMember, run.started_by_id)
    apply_until = (
        run.finished_at + DRY_RUN_TTL
        if run.dry_run and run.state == MigrationRunState.DONE and run.finished_at is not None
        else None
    )
    can_apply = (
        apply_until is not None
        and run.id in latest
        and run.id not in applied
        and not running
        and now < apply_until
    )
    return RunOut(
        id=run.id,
        kind=run.kind,
        dry_run=run.dry_run,
        state=run.state,
        dry_run_id=run.dry_run_id,
        applied_by_run_id=applied.get(run.id),
        mapping=[MappingRule.model_validate(rule) for rule in run.tariff_mapping],
        source=run.source,
        report=MigrationReport.model_validate(run.report) if run.report is not None else None,
        processed=run.processed,
        total=run.total,
        error=run.error,
        started_by_telegram_id=member.telegram_id if member is not None else None,
        started_at=run.started_at,
        finished_at=run.finished_at,
        can_apply=can_apply,
        apply_until=apply_until if can_apply else None,
    )


async def _views(session: AsyncSession, runs: list[MigrationRun]) -> list[RunOut]:
    latest = await _latest_dry_runs(session)
    applied = {
        dry_run_id: run_id
        for run_id, dry_run_id in await session.execute(
            select(MigrationRun.id, MigrationRun.dry_run_id).where(
                MigrationRun.dry_run_id.is_not(None)
            )
        )
        if dry_run_id is not None
    }
    running = (
        await session.scalar(select(MigrationRun.id).where(MigrationRun.finished_at.is_(None)))
        is not None
    )
    now = datetime.now(UTC)
    return [
        await _out(session, run, latest=latest, applied=applied, running=running, now=now)
        for run in runs
    ]


async def _view(session: AsyncSession, run: MigrationRun) -> RunOut:
    [view] = await _views(session, [run])
    return view


@router.get("/runs")
async def runs(session: DbSession, owner: Owner) -> RunsOut:
    """История прогонов, новые первыми (А9)."""
    del owner
    found = await session.scalars(
        select(MigrationRun).order_by(MigrationRun.id.desc()).limit(HISTORY_LIMIT)
    )
    return RunsOut(
        runs=await _views(session, list(found)),
        time_zone=(await shop_time_zone(session)).key,
    )


@router.get("/runs/{run_id}", responses={status.HTTP_404_NOT_FOUND: {}})
async def run(run_id: int, session: DbSession, owner: Owner) -> RunOut:
    del owner
    found = await session.get(MigrationRun, run_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MESSAGES[RunError.NOT_FOUND])
    return await _view(session, found)


@router.post(
    "/adoption",
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_409_CONFLICT: {}, status.HTTP_422_UNPROCESSABLE_CONTENT: {}},
)
async def adoption(body: MappingIn, session: DbSession, owner: Owner) -> RunOut:
    """Пробный прогон усыновления (9.1, 9.15)."""
    try:
        started = await start_dry_run(session, member_id=owner.id, mapping=body.mapping)
    except MigrationRunError as error:
        raise _error(error) from error
    await session.commit()
    return await _view(session, started)


@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_409_CONFLICT: {},
        status.HTTP_413_CONTENT_TOO_LARGE: {},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {},
    },
)
async def import_file(request: Request, session: DbSession, owner: Owner) -> RunOut:
    """Файл в промежуточном формате — телом запроса; сразу пробный прогон (9.10)."""
    data = await read_limited(request, MAX_FILE_BYTES)
    try:
        file = parse_file(data)
    except FormatError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "format", "message": str(error)}
        ) from error
    try:
        started = await start_dry_run(session, member_id=owner.id, mapping=[], file=file)
    except MigrationRunError as error:
        raise _error(error) from error
    await session.commit()
    return await _view(session, started)


@router.post(
    "/runs/{run_id}/rerun",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_409_CONFLICT: {},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {},
    },
)
async def rerun_dry(run_id: int, body: MappingIn, session: DbSession, owner: Owner) -> RunOut:
    """Пробный прогон заново — с новым сопоставлением тарифов (9.15)."""
    try:
        started = await rerun(session, run_id=run_id, member_id=owner.id, mapping=body.mapping)
    except MigrationRunError as error:
        raise _error(error) from error
    await session.commit()
    return await _view(session, started)


@router.post(
    "/runs/{run_id}/apply",
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_409_CONFLICT: {}},
)
async def apply(run_id: int, session: DbSession, owner: Owner) -> RunOut:
    """Применение пробного прогона (9.3)."""
    try:
        started = await apply_dry_run(
            session, run_id=run_id, member_id=owner.id, now=datetime.now(UTC)
        )
    except MigrationRunError as error:
        raise _error(error) from error
    await session.commit()
    return await _view(session, started)
