"""«Настройки → Триал»: включён ли триал и его параметры (5.1, 04-operator-settings)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from remnabay.panel import PanelClient, PanelError, PanelUnavailableError
from remnabay.tariffs import TariffError, check_squads
from remnabay.trial import (
    MAX_DEVICES,
    MAX_DURATION_DAYS,
    MAX_TRAFFIC_GB,
    TrialParams,
    TrialSettingsError,
    save_trial_settings,
    trial_settings,
)
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(prefix="/settings/trial", tags=["settings"])


class TrialSettingsOut(BaseModel):
    enabled: bool
    duration_days: int
    # 0 — без ограничения; трафик триала не сбрасывается (5.1)
    traffic_limit_gb: int
    device_limit: int
    squad_uuids: list[UUID]


class TrialSettingsIn(BaseModel):
    enabled: bool
    duration_days: Annotated[int, Field(ge=1, le=MAX_DURATION_DAYS)]
    traffic_limit_gb: Annotated[int, Field(ge=0, le=MAX_TRAFFIC_GB)]
    device_limit: Annotated[int, Field(ge=1, le=MAX_DEVICES)]
    squad_uuids: Annotated[list[UUID], Field(max_length=100)]


async def _out(session: DbSession) -> TrialSettingsOut:
    enabled, params = await trial_settings(session)
    return TrialSettingsOut(enabled=enabled, **params.model_dump())


@router.get("")
async def get_trial_settings(session: DbSession, owner: Owner) -> TrialSettingsOut:
    del owner
    return await _out(session)


@router.put(
    "",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {}, status.HTTP_502_BAD_GATEWAY: {}},
)
async def update_trial_settings(
    body: TrialSettingsIn, request: Request, session: DbSession, owner: Owner
) -> TrialSettingsOut:
    """5.1: сквады выбираются из списка панели; при включении — хотя бы один."""
    _enabled, current = await trial_settings(session)
    panel: PanelClient = request.app.state.panel
    try:
        await check_squads(panel, body.squad_uuids, already=current.squad_uuids)
    except PanelUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Панель недоступна — сквады не проверить"
        ) from error
    except PanelError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Панель ответила ошибкой: {error}"
        ) from error
    except TariffError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    params = TrialParams(
        duration_days=body.duration_days,
        traffic_limit_gb=body.traffic_limit_gb,
        device_limit=body.device_limit,
        squad_uuids=body.squad_uuids,
    )
    try:
        await save_trial_settings(session, body.enabled, params, member_id=owner.id)
    except TrialSettingsError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return await _out(session)
