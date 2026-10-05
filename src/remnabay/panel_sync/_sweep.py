"""Периодическая сверка всех подписок с панелью (4.8).

Вебхук может потеряться: панель повторяет его лишь несколько раз за ~15 секунд.
Сверка раз в интервал из настроек (по умолчанию 15 минут) ставит сверку каждой
подписки. Большая работа делится на страницы и небольшие операции (4.28).
"""

from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.subscriptions import Subscription, operations_key
from remnabay.panel_sync._outage import panel_available
from remnabay.panel_sync._reconcile import Source, enqueue_reconcile, reconcile_subscription
from remnabay.queue import Periodic, TaskContext, task, unfinished_keys
from remnabay.shop_settings import PANEL_SYNC_INTERVAL, get_setting

PAGE_SIZE = 500


class SyncPageArgs(BaseModel):
    """Страница подписок для сверки: после подписки `after_id`."""

    after_id: int = 0
    page_size: int = PAGE_SIZE


@task("panel.sync_page", SyncPageArgs)
async def sync_page(context: TaskContext, args: SyncPageArgs) -> None:
    """Ставит сверку подписок страницы и следующую страницу.

    Подписке, у которой сверка уже ждёт (например, за проваленной операцией, 4.27),
    вторая не ставится — иначе они копились бы каждые 15 минут.
    """
    session = context.session
    if not await panel_available(session):
        # Панель недоступна — сверять не с чем; следующая сверка будет через интервал
        return
    ids = list(
        await session.scalars(
            select(Subscription.id)
            .where(Subscription.id > args.after_id, Subscription.deleted_at.is_(None))
            .order_by(Subscription.id)
            .limit(args.page_size)
        )
    )
    keys = {operations_key(subscription_id): subscription_id for subscription_id in ids}
    waiting = await unfinished_keys(session, reconcile_subscription.name, keys)
    for key, subscription_id in keys.items():
        if key not in waiting:
            await enqueue_reconcile(session, subscription_id, Source.SYNC)
    if len(ids) == args.page_size:
        await sync_page.enqueue(session, SyncPageArgs(after_id=ids[-1], page_size=args.page_size))


async def sync_interval(session: AsyncSession) -> timedelta:
    """Интервал сверки — из настроек оператора, без перезапуска (4.8)."""
    return await get_setting(session, PANEL_SYNC_INTERVAL)


SYNC_ALL = Periodic(sync_page, SyncPageArgs(), sync_interval)
