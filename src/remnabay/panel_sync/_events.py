"""События вебхуков панели: однократность (4.2) и постановка сверки.

Подпись уже проверена (4.1). Событие сохраняется по хэшу тела: панель повторяет
доставку тем же телом, и повтор узнаётся без разбора содержимого (сквозное
правило 3). Само событие только подсказывает, какую подписку сверить: сверка
перечитывает пользователя из панели.
"""

import hashlib
import json

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.panel_events import PanelEvent as StoredEvent
from remnabay.domain.subscriptions import Subscription
from remnabay.journal import Actor, Outcome, record
from remnabay.panel import DeviceEvent, PanelEvent, UserEvent
from remnabay.panel_sync._reconcile import Source, enqueue_reconcile


def _panel_user_id(event: PanelEvent) -> int | None:
    match event:
        case UserEvent():
            return event.data.id
        case DeviceEvent():
            return event.data.user.id
        case _:
            return None


async def receive_event(session: AsyncSession, body: bytes, event: PanelEvent) -> None:
    """Принимает событие с верной подписью: повтор — только в журнал, новое — сверка
    подписки его пользователя."""
    payload = json.loads(body)
    name = str(payload.get("event", ""))
    stored_id = await session.scalar(
        insert(StoredEvent)
        .values(body_hash=hashlib.sha256(body).digest(), event=name[:64], payload=payload)
        .on_conflict_do_nothing()
        .returning(StoredEvent.id)
    )
    if stored_id is None:
        # Повтор того же события ничего не делает второй раз; факт повтора — в журнал
        await record(
            session,
            actor=Actor.PANEL,
            action="panel.event_repeated",
            outcome=Outcome.SUCCESS,
            details={"event": name},
        )
        return
    user_id = _panel_user_id(event)
    if user_id is not None:
        subscription_id = await session.scalar(
            select(Subscription.id).where(
                Subscription.panel_user_id == user_id, Subscription.deleted_at.is_(None)
            )
        )
        # Пользователь без подписки в магазине (создан в панели вручную) не усыновляется
        if subscription_id is not None:
            await enqueue_reconcile(session, subscription_id, Source.WEBHOOK, name)
    await session.execute(
        update(StoredEvent).where(StoredEvent.id == stored_id).values(processed_at=func.now())
    )


async def webhook_received(session: AsyncSession) -> bool:
    """Пришло ли хотя бы одно событие с верной подписью — пункт чек-листа «Вебхук панели» (1.9)."""
    return bool(await session.scalar(select(exists().select_from(StoredEvent))))
