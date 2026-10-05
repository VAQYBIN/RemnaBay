"""Недоступность панели: простой, одно уведомление на простой, «данные могут быть
неактуальны» (4.11–4.13, 4.30).

Раз в минуту магазин спрашивает у панели её версию. Нет ответа (соединение не
устанавливается, таймаут, ошибки шлюза) — простой начался; ответ есть — простой
закончился. Если простой длится дольше настройки (по умолчанию 5 минут), команда
получает одно уведомление. Операции тем временем «ждут панель» и продолжаются
сами (очередь), оплаты принимаются и применяются после восстановления (4.11).
"""

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import BigInteger, DateTime, Identity, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay import runtime
from remnabay.db import Base
from remnabay.journal import Actor, Outcome, record
from remnabay.messaging import load_texts, notify_team
from remnabay.panel import PanelError, PanelUnavailableError
from remnabay.queue import Periodic, TaskContext, task
from remnabay.shop_settings import (
    PANEL_OUTAGE_ALERT_AFTER,
    SHOP_LANGUAGE,
    get_setting,
    shop_time_zone,
)

HEALTH_CHECK_EVERY = timedelta(minutes=1)

logger = logging.getLogger(__name__)


class PanelOutage(Base):
    """Простой панели: с какого момента недоступна, когда вернулась, когда уведомили команду.

    Открытый простой — не больше одного: его открывает и закрывает только проверка
    связи, а она ставится раз в минуту одна на все воркеры.
    """

    __tablename__ = "panel_outages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


async def current_outage(session: AsyncSession) -> PanelOutage | None:
    """Открытый простой панели или `None`, если панель на связи."""
    return await session.scalar(
        select(PanelOutage).where(PanelOutage.ended_at.is_(None)).with_for_update()
    )


async def panel_available(session: AsyncSession) -> bool:
    """Панель на связи. Нет — клиент видит последние известные данные с пометкой
    «могут быть неактуальны» (4.12, `menu.stale_data`)."""
    open_outage = await session.scalar(
        select(func.count()).select_from(PanelOutage).where(PanelOutage.ended_at.is_(None))
    )
    return not open_outage


async def _started(session: AsyncSession) -> PanelOutage:
    outage = PanelOutage(started_at=datetime.now(UTC))
    session.add(outage)
    await session.flush()
    await record(session, actor=Actor.SYSTEM, action="panel.unavailable", outcome=Outcome.FAILURE)
    return outage


async def _alert_if_long(session: AsyncSession, outage: PanelOutage) -> None:
    """Одно уведомление на простой — когда он длится дольше настройки (4.13, 4.30)."""
    if outage.notified_at is not None:
        return
    now = datetime.now(UTC)
    if now - outage.started_at < await get_setting(session, PANEL_OUTAGE_ALERT_AFTER):
        return
    texts = await load_texts(session)
    since = texts.date_fallback(
        outage.started_at,
        await get_setting(session, SHOP_LANGUAGE),
        await shop_time_zone(session),
    )
    await notify_team(session, "team.panel_unavailable", variables={"since": since})
    outage.notified_at = now


class HealthCheckArgs(BaseModel):
    pass


@task("panel.health_check", HealthCheckArgs)
async def health_check(context: TaskContext, _args: HealthCheckArgs) -> None:
    session = context.session
    outage = await current_outage(session)
    try:
        await runtime.current().panel.get_metadata()
    except PanelUnavailableError:
        await _alert_if_long(session, outage or await _started(session))
        return
    except PanelError as error:
        # Панель ответила, но с ошибкой (например, неверный токен) — это не простой (4.30):
        # операции получат ту же ошибку и попадут в «Требуют внимания»
        logger.warning("Проверка связи с панелью: %s", error)
        return
    if outage is not None:
        outage.ended_at = datetime.now(UTC)
        await record(
            session,
            actor=Actor.SYSTEM,
            action="panel.available_again",
            outcome=Outcome.SUCCESS,
            details={"since": outage.started_at.isoformat()},
        )


HEALTH_CHECK = Periodic(health_check, HealthCheckArgs(), HEALTH_CHECK_EVERY)
