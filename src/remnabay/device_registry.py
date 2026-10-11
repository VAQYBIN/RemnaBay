"""Реестр устройств: факт подключения устройства к подписке (5.7–5.9, решение 0013).

Запись появляется по событию панели о подключении устройства к любой подписке, а
для триальных подписок — ещё и при сверке, если событие не дошло (5.8). Запись не
удаляется, когда устройство удаляют клиент или оператор в панели (5.9): история
нужна для проверки триала по устройству.
"""

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.subscriptions import Subscription
from remnabay.domain.trials import DeviceRecord
from remnabay.journal import Actor, Outcome, Subject, record

SUBSCRIPTION_SUBJECT = "subscription"


async def record_device(
    session: AsyncSession,
    subscription: Subscription,
    hwid: str,
    connected_at: datetime,
    *,
    source: str,
) -> bool:
    """Записывает подключение устройства; повтор ничего не делает (сквозное правило 3).
    `True` — запись новая."""
    added = await session.scalar(
        insert(DeviceRecord)
        .values(
            hwid=hwid,
            client_id=subscription.client_id,
            subscription_id=subscription.id,
            connected_at=connected_at,
        )
        .on_conflict_do_nothing(index_elements=[DeviceRecord.subscription_id, DeviceRecord.hwid])
        .returning(DeviceRecord.id)
    )
    if added is None:
        return False
    await record(
        session,
        actor=Actor.PANEL,
        action="device.registered",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details={"hwid": hwid, "source": source},
    )
    return True
