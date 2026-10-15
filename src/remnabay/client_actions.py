"""Действия команды из карточки клиента (сценарий «Найти клиента и продлить», 10.3–10.5, 10.7).

- Выдать дни: подписка, число дней и обязательный комментарий. Дни добавляются в
  панели и отрезком нулевой стоимости в магазине; истёкшей подписке — от момента
  выдачи (10.3). Изменение панели — операция очереди (4.14) в порядке операций
  подписки (4.17). После применения клиент получает «Добавлены дни» (10.4);
  комментарий виден только команде.
- Сбросить устройства подписки и синхронизировать её с панелью (10.5).
- Каждое действие — в журнале с участником и комментарием (10.7).

Права (01-domain, таблица ролей): всё это может и помощник.
"""

from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.devices import Removal, remove_devices
from remnabay.domain.clients import Client
from remnabay.domain.subscriptions import (
    SegmentKind,
    Subscription,
    TermSegment,
    operations_key,
)
from remnabay.domain.tariffs import Tariff, TariffType
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_BUTTON, load_texts, send_to_client
from remnabay.panel import PanelClient, PanelError, UpdateUser
from remnabay.panel_sync import (
    ReconcileArgs,
    Source,
    accept_access,
    accept_user,
    reconcile,
    term_end,
)
from remnabay.queue import TaskContext, task

SUBSCRIPTION_SUBJECT = "subscription"
# Пределы выдачи дней: защита от опечатки, например лишнего нуля (решение 0065)
MIN_GRANT_DAYS = 1
MAX_GRANT_DAYS = 3650
# Панель хранит дату с точностью до миллисекунд: даты ближе секунды — одна и та же
_SAME_MOMENT = timedelta(seconds=1)


class ClientActionError(Exception):
    """Действие недоступно; текст показывается участнику команды."""


def _subject(subscription_id: int) -> Subject:
    return Subject(SUBSCRIPTION_SUBJECT, subscription_id)


def _comment(comment: str) -> str:
    comment = comment.strip()
    if not comment:
        raise ClientActionError("Нужен комментарий: он попадает в журнал и карточку клиента")
    return comment


async def _subscription(
    session: AsyncSession, client_id: int, subscription_id: int
) -> Subscription:
    subscription = await session.get(Subscription, subscription_id)
    if subscription is None or subscription.client_id != client_id:
        raise ClientActionError("Подписка этого клиента не найдена")
    if subscription.deleted_at is not None:
        raise ClientActionError("Пользователя подписки удалили в панели")
    return subscription


async def has_term(session: AsyncSession, subscription: Subscription) -> bool:
    """Подписка со сроком: не «только пакет» и с датой окончания (решение 0065). Дни
    можно выдать и отключённой в панели."""
    if subscription.deleted_at is not None or subscription.expires_at is None:
        return False
    if subscription.tariff_id is None:
        return True
    tariff = await session.get(Tariff, subscription.tariff_id)
    return tariff is None or tariff.type != TariffType.PACKAGE_ONLY


# --- Выдать дни (10.3, 10.4) ---


class GrantArgs(BaseModel):
    subscription_id: int
    days: int
    # Момент выдачи: истёкшей подписке дни отсчитываются от него (10.3)
    granted_at: datetime
    member_id: int


async def grant_days(
    session: AsyncSession,
    client_id: int,
    subscription_id: int,
    *,
    days: int,
    comment: str,
    member_id: int,
    now: datetime,
) -> None:
    """Участник команды выдаёт дни (10.3). Дни добавятся в панели операцией очереди;
    выдача и комментарий — в журнале сразу (10.7)."""
    comment = _comment(comment)
    if not MIN_GRANT_DAYS <= days <= MAX_GRANT_DAYS:
        raise ClientActionError(f"Число дней — от {MIN_GRANT_DAYS} до {MAX_GRANT_DAYS}")
    subscription = await _subscription(session, client_id, subscription_id)
    if not await has_term(session, subscription):
        raise ClientActionError("Дни выдаются только подписке со сроком")
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="subscription.days_granted",
        outcome=Outcome.SUCCESS,
        subject=_subject(subscription.id),
        details={"client_id": client_id, "days": days, "comment": comment},
    )
    await add_granted_days.enqueue(
        session,
        GrantArgs(subscription_id=subscription.id, days=days, granted_at=now, member_id=member_id),
        key=operations_key(subscription.id),
    )


@task("subscriptions.grant_days", GrantArgs)
async def add_granted_days(context: TaskContext, args: GrantArgs) -> None:
    """Дни, выданные командой, — в панели и отрезком нулевой стоимости (10.3).

    Перед изменением панели сверяется её состояние: не добавлены ли дни прошлой
    попыткой (4.15); отрезок пишется после того, как панель приняла срок (4.34).
    Строка подписки блокируется до обращения к панели (4.17)."""
    session = context.session
    subscription = await session.get(Subscription, args.subscription_id, with_for_update=True)
    if subscription is None:
        return
    if subscription.deleted_at is not None:
        raise PanelError("Пользователя подписки удалили в панели — добавить дни некуда")
    panel = runtime.current().panel
    user = await panel.get_user(subscription.panel_user_id)
    if user is None:
        raise PanelError("Пользователя подписки удалили в панели — добавить дни некуда")
    days = timedelta(days=args.days)
    known_end = await term_end(session, subscription)
    expected_end = max(known_end or args.granted_at, args.granted_at) + days
    if abs(user.expire_at - expected_end) < _SAME_MOMENT:
        # Прошлая попытка уже добавила дни в панели, а магазин об этом не записал (4.15)
        start = expected_end - days
    else:
        await accept_user(session, subscription, user)
        # Истёкшая подписка — от момента выдачи (10.3)
        start = max(user.expire_at, args.granted_at)
        user = await panel.update_user(UpdateUser(id=user.id, expire_at=start + days))
    accept_access(subscription, user)
    session.add(
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.GRANTED,
            starts_at=start,
            ends_at=user.expire_at,
        )
    )
    await session.flush()
    await record(
        session,
        actor=Actor.SYSTEM,
        action="subscription.days_added",
        outcome=Outcome.SUCCESS,
        subject=_subject(subscription.id),
        details={
            "days": args.days,
            "granted_by": args.member_id,
            "expires_at": user.expire_at.isoformat(),
        },
    )
    client = await session.get(Client, subscription.client_id)
    texts = await load_texts(session)
    await send_to_client(
        session,
        subscription.client_id,
        "event.days_added",
        variables={
            "subscription_name": subscription.name,
            "days_text": texts.quantity(
                "unit.days", client.language_code if client else None, args.days
            ),
            # Подменяется датой из `dates`
            "end_date": "",
        },
        dates={"end_date": user.expire_at},
        buttons=[MAIN_MENU_BUTTON],
    )


@add_granted_days.on_cancelled
async def _grant_cancelled(session: AsyncSession, _task_id: int, args: GrantArgs) -> None:
    """4.32: команда отменила выдачу дней — дни не добавлены. Выдачу начала команда,
    поэтому клиенту ничего не отправляется: о сбое знает только она (0067)."""
    subscription = await session.get(Subscription, args.subscription_id)
    if subscription is None:
        return
    await record(
        session,
        actor=Actor.SYSTEM,
        action="subscription.days_grant_cancelled",
        outcome=Outcome.FAILURE,
        subject=_subject(subscription.id),
        details={"days": args.days, "granted_by": args.member_id},
    )


# --- Сбросить устройства, синхронизировать (10.5) ---


async def reset_devices(
    session: AsyncSession,
    panel: PanelClient,
    client_id: int,
    subscription_id: int,
    *,
    member_id: int,
) -> Removal:
    """Участник команды удаляет все устройства подписки (10.5). Панель недоступна —
    удаление в очереди и выполнится, когда она вернётся (4.14)."""
    subscription = await _subscription(session, client_id, subscription_id)
    return await remove_devices(session, panel, subscription, member_id=member_id)


async def sync_subscription(
    session: AsyncSession,
    panel: PanelClient,
    client_id: int,
    subscription_id: int,
    *,
    member_id: int,
) -> Subscription:
    """Немедленная сверка подписки с панелью по просьбе команды (10.5): по тем же
    правилам, что вебхук и периодическая сверка (4.3–4.7). Панель недоступна —
    `PanelUnavailableError` у вызывающего."""
    subscription = await _subscription(session, client_id, subscription_id)
    # Блокировка: применение платежа к этой подписке и сверка не идут одновременно
    await session.refresh(subscription, with_for_update=True)
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="subscription.sync_requested",
        outcome=Outcome.SUCCESS,
        subject=_subject(subscription.id),
    )
    args = ReconcileArgs(subscription_id=subscription.id, source=Source.SYNC, event="team")
    await reconcile(session, subscription, args, panel=panel)
    return subscription
