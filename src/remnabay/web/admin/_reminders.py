"""«Настройки → Напоминания»: расписания для платных подписок и для триала (7.1–7.3)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from remnabay.reminders import MAX_OFFSET_HOURS, ScheduleError, save_schedules, schedules
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(prefix="/settings/reminders", tags=["settings"])

# Часы до окончания; отрицательное — после окончания. 0 нет: в момент окончания
# приходит «Подписка истекла» или «Пробный период закончился» (0061)
type OffsetHours = Annotated[int, Field(ge=-MAX_OFFSET_HOURS, le=MAX_OFFSET_HOURS)]


class ReminderSettings(BaseModel):
    """Смещения по порядку моментов: от самого раннего до самого позднего."""

    paid: Annotated[list[OffsetHours], Field(max_length=100)]
    trial: Annotated[list[OffsetHours], Field(max_length=100)]


@router.get("")
async def get_reminder_settings(session: DbSession, owner: Owner) -> ReminderSettings:
    del owner
    current = await schedules(session)
    return ReminderSettings(paid=current.paid, trial=current.trial)


@router.put("")
async def update_reminder_settings(
    body: ReminderSettings, session: DbSession, owner: Owner
) -> ReminderSettings:
    """До 10 смещений в каждом расписании, повторяющиеся схлопываются (0061); запланированные
    напоминания пересчитываются без перезапуска (7.3)."""
    try:
        saved = await save_schedules(session, body.paid, body.trial, member_id=owner.id)
    except ScheduleError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return ReminderSettings(paid=saved.paid, trial=saved.trial)
