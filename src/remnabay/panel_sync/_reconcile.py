"""Сверка подписки с панелью: панель всегда права (сценарий «Реакция на изменения в панели»).

Одна сверка для обоих каналов — вебхука и периодического обхода (4.8). Она не
доверяет содержимому события, а перечитывает пользователя из панели: события
приходят с опозданием и не по порядку, а текущее состояние — одно. Поэтому
последствия применяются, только если они ещё актуальны (4.10).

Сверка — операция очереди с ключом подписки: она выполняется строго после
операций магазина над этой подпиской (4.17) и видит уже применённые ими отрезки.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.subscriptions import (
    PanelUserStatus,
    SegmentKind,
    Subscription,
    TermSegment,
    operations_key,
)
from remnabay.domain.tariffs import TrafficResetStrategy
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.messaging import ButtonArgs, send_to_client
from remnabay.panel import TrafficStrategy, User, UserStatus
from remnabay.queue import TaskContext, task

SUBSCRIPTION_SUBJECT = "subscription"

_STATUSES = {
    UserStatus.ACTIVE: PanelUserStatus.ACTIVE,
    UserStatus.DISABLED: PanelUserStatus.DISABLED,
    UserStatus.LIMITED: PanelUserStatus.LIMITED,
    UserStatus.EXPIRED: PanelUserStatus.EXPIRED,
}
STRATEGIES: dict[TrafficStrategy, TrafficResetStrategy | None] = {
    TrafficStrategy.NO_RESET: None,
    TrafficStrategy.DAY: TrafficResetStrategy.DAY,
    TrafficStrategy.WEEK: TrafficResetStrategy.WEEK,
    TrafficStrategy.MONTH: TrafficResetStrategy.MONTH,
    TrafficStrategy.MONTH_ROLLING: TrafficResetStrategy.MONTH_FROM_CREATION,
}


class Source(StrEnum):
    """Каким путём магазин узнал об изменении (4.9)."""

    WEBHOOK = "webhook"
    SYNC = "sync"


class ReconcileArgs(BaseModel):
    subscription_id: int
    source: Source
    # Событие панели, из-за которого сверка поставлена (для журнала)
    event: str | None = None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


async def _term_end(session: AsyncSession, subscription: Subscription) -> datetime | None:
    """Где магазин считает конец срока: конец последнего отрезка, без отрезков — данные панели."""
    end = await session.scalar(
        select(func.max(TermSegment.ends_at)).where(TermSegment.subscription_id == subscription.id)
    )
    return end or subscription.expires_at


async def _extend_term(
    session: AsyncSession, subscription: Subscription, start: datetime, end: datetime
) -> None:
    """Дата окончания увеличена в панели — отрезок нулевой стоимости (4.3)."""
    session.add(
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.PANEL,
            starts_at=start,
            ends_at=end,
        )
    )
    await session.flush()


async def _cut_term(session: AsyncSession, subscription: Subscription, end: datetime) -> None:
    """Дата окончания уменьшена в панели — отрезки срезаются с конца (4.4)."""
    of_subscription = TermSegment.subscription_id == subscription.id
    await session.execute(delete(TermSegment).where(of_subscription, TermSegment.starts_at >= end))
    await session.execute(
        update(TermSegment).where(of_subscription, TermSegment.ends_at > end).values(ends_at=end)
    )


async def _apply_term(
    session: AsyncSession, subscription: Subscription, user: User
) -> dict[str, JsonValue]:
    known_end = await _term_end(session, subscription)
    panel_end = user.expire_at
    if known_end == panel_end:
        return {}
    if known_end is None or panel_end > known_end:
        start = known_end or subscription.created_at
        if start < panel_end:
            await _extend_term(session, subscription, start, panel_end)
    else:
        await _cut_term(session, subscription, panel_end)
    return {"expires_at": [_iso(known_end), _iso(panel_end)]}


def _apply_access(subscription: Subscription, user: User) -> dict[str, JsonValue]:
    """Лимиты, стратегия сброса и состояние — как факт (4.5, 4.7)."""
    changes: dict[str, JsonValue] = {}
    status = _STATUSES.get(user.status, subscription.panel_status)
    if status != subscription.panel_status:
        changes["status"] = [subscription.panel_status, status]
        subscription.panel_status = status
    if user.hwid_device_limit != subscription.device_limit:
        changes["device_limit"] = [subscription.device_limit, user.hwid_device_limit]
        subscription.device_limit = user.hwid_device_limit
    if user.traffic_limit_bytes != subscription.traffic_limit_bytes:
        changes["traffic_limit_bytes"] = [
            subscription.traffic_limit_bytes,
            user.traffic_limit_bytes,
        ]
        subscription.traffic_limit_bytes = user.traffic_limit_bytes
    if user.traffic_limit_strategy in STRATEGIES:
        strategy = STRATEGIES[user.traffic_limit_strategy]
        if strategy != subscription.traffic_reset_strategy:
            changes["traffic_reset_strategy"] = [subscription.traffic_reset_strategy, strategy]
            subscription.traffic_reset_strategy = strategy
    return changes


async def _apply_link(
    session: AsyncSession, subscription: Subscription, user: User
) -> dict[str, JsonValue]:
    """Ссылку отозвали в панели — новая ссылка и сообщение клиенту (4.29).

    Отзыв меняет короткий идентификатор пользователя. Если поменялся только адрес
    ссылки (оператор сменил домен подписок в панели), ссылка обновляется без
    сообщения: старая при этом не перестала работать по вине клиента.
    """
    revoked = user.short_uuid != subscription.panel_short_uuid
    if not revoked and user.subscription_url == subscription.subscription_url:
        return {}
    changes: dict[str, JsonValue] = {
        "subscription_url": [subscription.subscription_url, user.subscription_url]
    }
    subscription.panel_short_uuid = user.short_uuid
    subscription.subscription_url = user.subscription_url
    if revoked:
        changes["link_revoked"] = True
        await send_to_client(
            session,
            subscription.client_id,
            "event.link_revoked",
            variables={
                "subscription_name": subscription.name,
                "subscription_link": user.subscription_url,
            },
            buttons=[ButtonArgs(text_key="btn.copy_link", copy_text=user.subscription_url)],
        )
    return changes


async def _journal(
    session: AsyncSession,
    subscription: Subscription,
    action: str,
    args: ReconcileArgs,
    changes: dict[str, JsonValue] | None = None,
) -> None:
    details: dict[str, JsonValue] = {"source": args.source.value, "event": args.event}
    if changes is not None:
        details["changes"] = changes
    await record(
        session,
        actor=Actor.PANEL,
        action=action,
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details=details,
    )


async def accept_user(
    session: AsyncSession, subscription: Subscription, user: User
) -> dict[str, JsonValue]:
    """Принимает пользователя панели как факт: срок (отрезки), лимиты, состояние, ссылка.

    Возвращает изменения для журнала. Общий шаг сверки и применения платежа: магазин
    сначала узнаёт, что сейчас в панели, и только потом меняет её сам."""
    changes = await _apply_term(session, subscription, user)
    changes |= _apply_access(subscription, user)
    changes |= await _apply_link(session, subscription, user)
    subscription.expires_at = user.expire_at
    subscription.traffic_used_bytes = user.user_traffic.used_traffic_bytes
    subscription.panel_synced_at = datetime.now(UTC)
    return changes


def accept_access(subscription: Subscription, user: User) -> None:
    """Лимиты, состояние, ссылка и срок из ответа панели на изменение, которое сделал
    сам магазин: отрезки он пишет сам (4.34), сообщений об этом клиенту не нужно."""
    _apply_access(subscription, user)
    subscription.panel_short_uuid = user.short_uuid
    subscription.subscription_url = user.subscription_url
    subscription.expires_at = user.expire_at
    subscription.traffic_used_bytes = user.user_traffic.used_traffic_bytes
    subscription.panel_synced_at = datetime.now(UTC)


async def term_end(session: AsyncSession, subscription: Subscription) -> datetime | None:
    """Конец срока по данным магазина: конец последнего отрезка или данные панели."""
    return await _term_end(session, subscription)


async def reconcile(session: AsyncSession, subscription: Subscription, args: ReconcileArgs) -> None:
    """Приводит знание магазина о подписке к состоянию панели и пишет изменения в журнал."""
    if subscription.deleted_at is not None:
        return
    user = await runtime.current().panel.get_user(subscription.panel_user_id)
    if user is None:
        # Пользователя удалили в панели: клиент подписку не видит, история сохраняется (4.6)
        subscription.deleted_at = datetime.now(UTC)
        await _journal(session, subscription, "subscription.deleted_in_panel", args)
        return
    changes = await accept_user(session, subscription, user)
    if changes:
        await _journal(session, subscription, "subscription.changed_in_panel", args, changes)


@task("panel.reconcile_subscription", ReconcileArgs, needs_attention=False)
async def reconcile_subscription(context: TaskContext, args: ReconcileArgs) -> None:
    subscription = await context.session.get(Subscription, args.subscription_id)
    if subscription is not None:
        await reconcile(context.session, subscription, args)


async def enqueue_reconcile(
    session: AsyncSession, subscription_id: int, source: Source, event: str | None = None
) -> int:
    """Ставит сверку подписки после всех её операций (4.17)."""
    return await reconcile_subscription.enqueue(
        session,
        ReconcileArgs(subscription_id=subscription_id, source=source, event=event),
        key=operations_key(subscription_id),
    )
