"""Дни по промокоду (8.10, 8.11, 8.14, 8.15, решения 0033, 0065).

- Дни добавляются отрезком нулевой стоимости к подписке, которую клиент выбрал;
  если подписка истекла — от момента активации (8.10).
- Активация засчитывается сразу: она занимает место в лимите активаций и не даёт
  клиенту ввести код второй раз, пока дни ждут панель (8.15).
- Панель доступна — дни добавляются сразу; нет — операция очереди, клиент видит
  «Дни будут добавлены в ближайшие минуты» и получает подтверждение после
  применения (8.15). Команда отменила операцию (4.32) — активация не расходуется.
- Перед изменением панели магазин сверяет её состояние: не добавлены ли уже дни
  прошлой попыткой (4.15). Отрезок пишется после того, как панель приняла срок (4.34).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.clients import Client
from remnabay.domain.promo import ActivationState, PromoActivation, PromoCode, PromoKind
from remnabay.domain.subscriptions import (
    SegmentKind,
    Subscription,
    TermSegment,
    operations_key,
)
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_BUTTON, load_texts, send_to_client, tell_action_failed
from remnabay.panel import PanelClient, PanelError, UpdateUser
from remnabay.panel_sync import accept_access, accept_user, panel_available, term_end
from remnabay.promo._codes import PROMO_SUBJECT
from remnabay.promo._entry import (
    PromoRejectedError,
    Rejection,
    check_days,
    days_targets,
    promo_entry,
    still_usable,
)
from remnabay.queue import TaskContext, task

SUBSCRIPTION_SUBJECT = "subscription"
# Панель хранит дату с точностью до миллисекунд: даты ближе секунды — одна и та же
_SAME_MOMENT = timedelta(seconds=1)


class DaysAdded(StrEnum):
    # Дни уже в панели (8.10)
    APPLIED = "applied"
    # Панель недоступна — в очереди (8.15)
    QUEUED = "queued"


@dataclass(frozen=True)
class DaysOutcome:
    result: DaysAdded
    subscription: Subscription
    days: int


class DaysArgs(BaseModel):
    activation_id: int


async def activate_days(
    session: AsyncSession,
    panel: PanelClient,
    client: Client,
    promo_id: int,
    subscription_id: int,
    now: datetime,
) -> DaysOutcome:
    """Клиент выбрал подписку для дней по промокоду (8.10). Код проверяется ещё раз:
    между вводом и выбором подписки его могли отключить или исчерпать.

    `PromoRejectedError` — код больше не подходит или подписка — не та."""
    # Клиент и промокод блокируются: одна активация на клиента и лимит активаций (8.2)
    await session.refresh(client, with_for_update=True)
    entry = await promo_entry(session, client.id)
    promo = await session.get(PromoCode, promo_id, with_for_update=True)
    if (
        # Только код, который клиент ввёл сам: номер в кнопке можно подделать
        entry.accepted_promo_id != promo_id
        or promo is None
        or promo.kind != PromoKind.DAYS
        or promo.days is None
        or not await still_usable(session, promo, now)
    ):
        raise PromoRejectedError(Rejection.INVALID)
    await check_days(session, client, promo)
    targets = {
        subscription.id: subscription for subscription in await days_targets(session, client.id)
    }
    subscription = targets.get(subscription_id)
    if subscription is None:
        raise PromoRejectedError(Rejection.NO_SUBSCRIPTION)
    activation = PromoActivation(
        promo_code_id=promo.id,
        client_id=client.id,
        subscription_id=subscription.id,
        state=ActivationState.PENDING,
        created_at=now,
    )
    session.add(activation)
    entry.accepted_promo_id = None
    await session.flush()
    await record(
        session,
        actor=Actor.client(client.id),
        action="promo.activated",
        outcome=Outcome.SUCCESS,
        subject=Subject(PROMO_SUBJECT, promo.id),
        details={
            "client_id": client.id,
            "subscription_id": subscription.id,
            "days": promo.days,
            "activation_id": activation.id,
        },
    )
    if await panel_available(session):
        try:
            async with session.begin_nested():
                await add_days(session, panel, activation, promo.days)
        except PanelError:
            # Панель не ответила или отказала — дни добавит очередь с повторами (8.15)
            pass
        else:
            return DaysOutcome(DaysAdded.APPLIED, subscription, promo.days)
    await add_promo_days.enqueue(
        session, DaysArgs(activation_id=activation.id), key=operations_key(subscription.id)
    )
    return DaysOutcome(DaysAdded.QUEUED, subscription, promo.days)


async def add_days(
    session: AsyncSession, panel: PanelClient, activation: PromoActivation, days_count: int
) -> Subscription:
    """Добавляет дни в панели и отрезок нулевой стоимости в магазине (8.10).

    Строка подписки блокируется до обращения к панели: сверка и другие операции
    подписки ждут, пока отрезок не записан (4.17)."""
    if activation.subscription_id is None:
        raise ValueError("Активация дней без подписки")
    subscription = await session.get(Subscription, activation.subscription_id)
    if subscription is None:
        raise ValueError("Подписка активации не найдена")
    await session.refresh(subscription, with_for_update=True)
    user = await panel.get_user(subscription.panel_user_id)
    if user is None:
        raise PanelError("Пользователя подписки удалили в панели — добавить дни некуда")
    days = timedelta(days=days_count)
    activated_at = activation.created_at
    known_end = await term_end(session, subscription)
    expected_end = max(known_end or activated_at, activated_at) + days
    if abs(user.expire_at - expected_end) < _SAME_MOMENT:
        # Прошлая попытка уже добавила дни в панели, а магазин об этом не записал (4.15)
        start = expected_end - days
    else:
        await accept_user(session, subscription, user)
        # Истёкшая подписка — от момента активации (8.10)
        start = max(user.expire_at, activated_at)
        user = await panel.update_user(UpdateUser(id=user.id, expire_at=start + days))
    accept_access(subscription, user)
    session.add(
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.PROMO_CODE,
            starts_at=start,
            ends_at=user.expire_at,
        )
    )
    activation.state = ActivationState.APPLIED
    activation.applied_at = datetime.now(UTC)
    await session.flush()
    await record(
        session,
        actor=Actor.SYSTEM,
        action="subscription.days_added",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details={
            "promo_code_id": activation.promo_code_id,
            "activation_id": activation.id,
            "days": days_count,
            "expires_at": user.expire_at.isoformat(),
        },
    )
    return subscription


@task("promo.add_days", DaysArgs)
async def add_promo_days(context: TaskContext, args: DaysArgs) -> None:
    """Дни по промокоду, отложенные до возвращения панели (8.15). После применения
    клиент получает подтверждение. Уже применённая активация — ничего второй раз."""
    session = context.session
    activation = await session.get(PromoActivation, args.activation_id, with_for_update=True)
    if activation is None or activation.state != ActivationState.PENDING:
        return
    promo = await session.get(PromoCode, activation.promo_code_id)
    if promo is None or promo.days is None:
        return
    subscription = await add_days(session, runtime.current().panel, activation, promo.days)
    client = await session.get(Client, activation.client_id)
    if client is None or subscription.expires_at is None:
        return
    texts = await load_texts(session)
    await send_to_client(
        session,
        client.id,
        "promo.days_applied",
        variables={
            "subscription_name": subscription.name,
            "days_text": texts.quantity("unit.days", client.language_code, promo.days),
            # Подменяется датой из `dates`
            "end_date": "",
        },
        dates={"end_date": subscription.expires_at},
        buttons=[MAIN_MENU_BUTTON],
    )


@add_promo_days.on_cancelled
async def _days_cancelled(session: AsyncSession, _task_id: int, args: DaysArgs) -> None:
    """4.32: команда отменила добавление дней — активация не расходуется, клиент
    получает «Не удалось выполнить действие»."""
    activation = await session.get(PromoActivation, args.activation_id, with_for_update=True)
    if activation is None or activation.state != ActivationState.PENDING:
        return
    activation.state = ActivationState.CANCELLED
    await record(
        session,
        actor=Actor.SYSTEM,
        action="promo.activation_cancelled",
        outcome=Outcome.FAILURE,
        subject=Subject(PROMO_SUBJECT, activation.promo_code_id),
        details={"client_id": activation.client_id, "activation_id": activation.id},
    )
    await tell_action_failed(session, activation.client_id)
