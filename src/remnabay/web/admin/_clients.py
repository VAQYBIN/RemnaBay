"""Клиенты и карточка клиента (А2, А3; 10.1–10.7).

Найти клиента, выдать дни, сбросить устройства, синхронизировать подписку и
зафиксировать запрос на возврат может любой участник команды; отказать в возврате —
только владелец (01-domain, таблица ролей; решение 0065). Каждое действие — в журнале
с участником и комментарием (10.7).
"""

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from remnabay.client_actions import (
    MAX_GRANT_DAYS,
    MIN_GRANT_DAYS,
    ClientActionError,
    add_granted_days,
    grant_days,
    has_term,
    reset_devices,
    sync_subscription,
)
from remnabay.client_card import (
    CARD_LIMIT,
    changes_waiting,
    journal_of,
    nearest_end,
    payments_of,
    referrals,
    refund_requests_of,
    search_clients,
    subscriptions_of,
    trial_of,
)
from remnabay.devices import app_name, delete_devices, list_devices, remember_device_limit
from remnabay.domain.clients import Client
from remnabay.domain.payments import Refund, RefundRequest, RefundRequestState
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember
from remnabay.journal import ActorType, JournalEntry, JsonValue, Outcome
from remnabay.panel import PanelClient, PanelError, PanelUnavailableError
from remnabay.panel_sync import panel_available
from remnabay.payments import apply_payment
from remnabay.promo import add_promo_days
from remnabay.refunds import RefundRequestError, change_term, decline_request, file_request
from remnabay.shop_settings import shop_time_zone
from remnabay.web.admin._deps import DbSession, Member, Owner
from remnabay.web.admin._payments import (
    REJECTED,
    ClientRef,
    CommentIn,
    PaymentRow,
    client_ref,
    known_client_ref,
    payment_row,
    rejected,
)

router = APIRouter(tags=["clients"])

# Ответ панели на список устройств — не дольше; иначе карточка покажет, что панель
# не ответила
PANEL_TIMEOUT_SECONDS = 5.0
# Изменения подписки в очереди, о которых команде полезно знать в карточке
_CHANGES = (
    add_granted_days.name,
    change_term.name,
    apply_payment.name,
    delete_devices.name,
    add_promo_days.name,
)


# --- Список (А2, 10.1) ---


class ClientRowOut(BaseModel):
    client: ClientRef
    subscriptions: int
    nearest_end: datetime | None
    bot_blocked: bool
    adopted: bool


class ClientsOut(BaseModel):
    clients: list[ClientRowOut]
    # Даты в админке — в часовом поясе магазина (1.18)
    time_zone: str


@router.get("/clients")
async def clients_list(
    session: DbSession,
    member: Member,
    q: Annotated[str, Query(max_length=512)] = "",
) -> ClientsOut:
    """А2: поиск по Telegram ID, username, имени, ссылке подписки или имени
    пользователя в панели (10.1); без строки поиска — последние клиенты."""
    del member
    clients = await search_clients(session, q)
    subscriptions = await subscriptions_of(session, [client.id for client in clients])
    now = datetime.now(UTC)
    rows: list[ClientRowOut] = []
    for client in clients:
        ref = await known_client_ref(session, client.id)
        own = subscriptions[client.id]
        rows.append(
            ClientRowOut(
                client=ref,
                subscriptions=sum(1 for s in own if s.deleted_at is None),
                nearest_end=nearest_end(own, now),
                bot_blocked=client.bot_blocked_at is not None,
                adopted=client.adopted_at is not None,
            )
        )
    return ClientsOut(clients=rows, time_zone=(await shop_time_zone(session)).key)


# --- Карточка (А3, 10.2) ---


class SubscriptionOut(BaseModel):
    id: int
    name: str
    tariff_name: str | None
    is_trial: bool
    # Последнее известное состояние из панели (0005, 4.12)
    status: PanelUserStatus | None
    expires_at: datetime | None
    device_limit: int | None
    panel_username: str
    subscription_url: str
    synced_at: datetime | None
    # Пользователя удалили в панели: клиент подписку не видит (4.6)
    deleted: bool
    # Подписке со сроком можно выдать дни (10.3)
    can_grant: bool
    # Изменения подписки, которые ещё в очереди: например, дни ждут панель
    changes_waiting: int


class TrialOut(BaseModel):
    used_at: datetime
    subscription_id: int | None
    imported: bool
    revoked_at: datetime | None


class RefundRequestOut(BaseModel):
    id: int
    state: RefundRequestState
    comment: str
    created_at: datetime
    created_by: str
    closed_at: datetime | None
    closed_by: str | None
    close_comment: str | None
    # Платёж, возврат по которому закрыл запрос
    payment_id: int | None


class JournalOut(BaseModel):
    occurred_at: datetime
    actor_type: ActorType
    # Участник команды — по имени (10.7)
    actor: str | None
    action: str
    outcome: Outcome
    subject_type: str | None
    subject_id: str | None
    details: dict[str, JsonValue]


class ClientCardOut(BaseModel):
    client: ClientRef
    first_seen_at: datetime | None
    created_at: datetime
    bot_blocked: bool
    adopted: bool
    subscriptions: list[SubscriptionOut]
    payments: list[PaymentRow]
    trial: TrialOut | None
    inviter: ClientRef | None
    invited: list[ClientRef]
    refund_requests: list[RefundRequestOut]
    journal: list[JournalOut]
    # Сколько последних записей журнала отдаёт карточка
    journal_limit: int = CARD_LIMIT
    # Панель недоступна — состояние подписок может быть неактуальным (4.12)
    panel_available: bool
    min_grant_days: int = MIN_GRANT_DAYS
    max_grant_days: int = MAX_GRANT_DAYS
    time_zone: str


def member_label(member: TeamMember) -> str:
    return member.name or f"ID {member.telegram_id}"


async def _members(session: DbSession) -> dict[int, str]:
    rows = await session.scalars(select(TeamMember))
    return {member.id: member_label(member) for member in rows}


async def _subscription_out(
    session: DbSession, subscription: Subscription, waiting: dict[int, int]
) -> SubscriptionOut:
    tariff = (
        await session.get(Tariff, subscription.tariff_id)
        if subscription.tariff_id is not None
        else None
    )
    return SubscriptionOut(
        id=subscription.id,
        name=subscription.name,
        tariff_name=tariff.name if tariff is not None else None,
        is_trial=subscription.is_trial,
        status=subscription.panel_status,
        expires_at=subscription.expires_at,
        device_limit=subscription.device_limit,
        panel_username=subscription.panel_username,
        subscription_url=subscription.subscription_url,
        synced_at=subscription.panel_synced_at,
        deleted=subscription.deleted_at is not None,
        can_grant=await has_term(session, subscription),
        changes_waiting=waiting.get(subscription.id, 0),
    )


async def _refund_payment_ids(session: DbSession, requests: list[RefundRequest]) -> dict[int, int]:
    refund_ids = [r.refund_id for r in requests if r.refund_id is not None]
    if not refund_ids:
        return {}
    rows = await session.execute(
        select(Refund.id, Refund.payment_id).where(Refund.id.in_(refund_ids))
    )
    return {refund_id: payment_id for refund_id, payment_id in rows}


def _actor(entry: JournalEntry, members: dict[int, str]) -> str | None:
    if entry.actor_type == ActorType.TEAM_MEMBER and entry.actor_ref is not None:
        return members.get(int(entry.actor_ref), f"участник {entry.actor_ref}")
    return entry.actor_ref


async def _card(session: DbSession, client_id: int) -> ClientCardOut:
    client = await session.get(Client, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Клиент не найден")
    subscriptions = (await subscriptions_of(session, [client.id]))[client.id]
    waiting = await changes_waiting(session, [s.id for s in subscriptions], _CHANGES)
    payments = await payments_of(session, client.id)
    trial = await trial_of(session, client.id)
    links = await referrals(session, client.id)
    requests = await refund_requests_of(session, client.id)
    refunded = await _refund_payment_ids(session, requests)
    members = await _members(session)
    journal = await journal_of(
        session, client.id, [s.id for s in subscriptions], [p.id for p in payments]
    )
    ref = await known_client_ref(session, client.id)
    return ClientCardOut(
        client=ref,
        first_seen_at=client.first_seen_at,
        created_at=client.created_at,
        bot_blocked=client.bot_blocked_at is not None,
        adopted=client.adopted_at is not None,
        # Сначала те, что клиент видит; удалённые в панели — в конце
        subscriptions=[
            await _subscription_out(session, s, waiting)
            for s in sorted(subscriptions, key=lambda s: (s.deleted_at is not None, s.id))
        ],
        payments=[await payment_row(session, payment) for payment in payments],
        trial=TrialOut(
            used_at=trial.used_at,
            subscription_id=trial.subscription_id,
            imported=trial.imported,
            revoked_at=trial.revoked_at,
        )
        if trial is not None
        else None,
        inviter=await client_ref(session, links.inviter_id),
        invited=[await known_client_ref(session, invited_id) for invited_id in links.invited_ids],
        refund_requests=[
            RefundRequestOut(
                id=r.id,
                state=r.state,
                comment=r.comment,
                created_at=r.created_at,
                created_by=members.get(r.created_by_id, "—"),
                closed_at=r.closed_at,
                closed_by=members.get(r.closed_by_id) if r.closed_by_id is not None else None,
                close_comment=r.close_comment,
                payment_id=refunded.get(r.refund_id) if r.refund_id is not None else None,
            )
            for r in requests
        ],
        journal=[
            JournalOut(
                occurred_at=entry.occurred_at,
                actor_type=entry.actor_type,
                actor=_actor(entry, members),
                action=entry.action,
                outcome=entry.outcome,
                subject_type=entry.subject_type,
                subject_id=entry.subject_id,
                details=entry.details,
            )
            for entry in journal
        ],
        panel_available=await panel_available(session),
        time_zone=(await shop_time_zone(session)).key,
    )


@router.get("/clients/{client_id}", responses={status.HTTP_404_NOT_FOUND: {}})
async def client_card(client_id: int, session: DbSession, member: Member) -> ClientCardOut:
    """А3: подписки с состоянием из панели, платежи, триал, рефералы, пометки,
    запросы на возврат и журнал по клиенту (10.2)."""
    del member
    return await _card(session, client_id)


# --- Устройства подписки (10.2) ---


class DeviceOut(BaseModel):
    hwid: str
    platform: str | None
    os_version: str | None
    model: str | None
    app: str | None
    connected_at: datetime


class DevicesOut(BaseModel):
    # Лимит устройств в панели выключен — раздела нет (6.1)
    enabled: bool
    devices: list[DeviceOut]
    limit: int | None


@router.get(
    "/clients/{client_id}/subscriptions/{subscription_id}/devices",
    responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_503_SERVICE_UNAVAILABLE: {}},
)
async def subscription_devices(
    client_id: int, subscription_id: int, request: Request, session: DbSession, member: Member
) -> DevicesOut:
    """Устройства подписки — из панели при открытии (6.2): у магазина своих данных нет."""
    del member
    subscription = await session.get(Subscription, subscription_id)
    if subscription is None or subscription.client_id != client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подписка не найдена")
    panel: PanelClient = request.app.state.panel
    try:
        async with asyncio.timeout(PANEL_TIMEOUT_SECONDS):
            limit = await remember_device_limit(session, await panel.get_subscription_settings())
            if not limit.enabled:
                await session.commit()
                return DevicesOut(enabled=False, devices=[], limit=None)
            devices = await list_devices(panel, subscription, limit)
    except (PanelError, TimeoutError) as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Панель не ответила — устройства не получены"
        ) from error
    await session.commit()
    return DevicesOut(
        enabled=True,
        devices=[
            DeviceOut(
                hwid=device.hwid,
                platform=device.platform,
                os_version=device.os_version,
                model=device.device_model,
                app=app_name(device),
                connected_at=device.created_at,
            )
            for device in devices.devices
        ],
        limit=devices.limit,
    )


# --- Действия (10.3–10.6) ---


class GrantIn(BaseModel):
    days: Annotated[int, Field(ge=MIN_GRANT_DAYS, le=MAX_GRANT_DAYS)]
    comment: Annotated[str, Field(min_length=1, max_length=2000)]


@router.post("/clients/{client_id}/subscriptions/{subscription_id}/grant-days", responses=REJECTED)
async def grant(
    client_id: int, subscription_id: int, body: GrantIn, session: DbSession, member: Member
) -> ClientCardOut:
    """«Выдать дни» с обязательным комментарием (10.3); клиент получит уведомление,
    когда дни применятся в панели (10.4)."""
    try:
        await grant_days(
            session,
            client_id,
            subscription_id,
            days=body.days,
            comment=body.comment,
            member_id=member.id,
            now=datetime.now(UTC),
        )
    except ClientActionError as error:
        raise rejected(error) from error
    await session.commit()
    return await _card(session, client_id)


class ResetOut(BaseModel):
    # Панель недоступна — удаление выполнится, когда она вернётся (4.14)
    result: Literal["done", "queued"]
    card: ClientCardOut


@router.post(
    "/clients/{client_id}/subscriptions/{subscription_id}/reset-devices", responses=REJECTED
)
async def reset(
    client_id: int, subscription_id: int, request: Request, session: DbSession, member: Member
) -> ResetOut:
    """«Сбросить устройства» — удалить все устройства подписки в панели (10.5)."""
    panel: PanelClient = request.app.state.panel
    try:
        removal = await reset_devices(
            session, panel, client_id, subscription_id, member_id=member.id
        )
    except ClientActionError as error:
        raise rejected(error) from error
    await session.commit()
    return ResetOut(result=removal.value, card=await _card(session, client_id))


@router.post(
    "/clients/{client_id}/subscriptions/{subscription_id}/sync",
    responses={**REJECTED, status.HTTP_503_SERVICE_UNAVAILABLE: {}},
)
async def sync(
    client_id: int, subscription_id: int, request: Request, session: DbSession, member: Member
) -> ClientCardOut:
    """«Синхронизировать с панелью» — немедленная сверка подписки (10.5)."""
    panel: PanelClient = request.app.state.panel
    try:
        await sync_subscription(session, panel, client_id, subscription_id, member_id=member.id)
    except ClientActionError as error:
        raise rejected(error) from error
    except PanelUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Панель недоступна — синхронизировать сейчас нельзя",
        ) from error
    except PanelError as error:
        raise rejected(error) from error
    await session.commit()
    return await _card(session, client_id)


@router.post("/clients/{client_id}/refund-requests", responses=REJECTED)
async def file_refund_request(
    client_id: int, body: CommentIn, session: DbSession, member: Member
) -> ClientCardOut:
    """«Зафиксировать запрос» на возврат с комментарием (10.6)."""
    try:
        await file_request(session, client_id, comment=body.comment, member_id=member.id)
    except RefundRequestError as error:
        raise rejected(error) from error
    await session.commit()
    return await _card(session, client_id)


@router.post("/clients/{client_id}/refund-requests/{request_id}/decline", responses=REJECTED)
async def decline_refund_request(
    client_id: int, request_id: int, body: CommentIn, session: DbSession, owner: Owner
) -> ClientCardOut:
    """«Отказать» в возврате с комментарием — только владелец (10.6, решение 0065)."""
    try:
        await decline_request(
            session,
            client_id,
            request_id,
            comment=body.comment,
            member_id=owner.id,
            now=datetime.now(UTC),
        )
    except RefundRequestError as error:
        raise rejected(error) from error
    await session.commit()
    return await _card(session, client_id)
