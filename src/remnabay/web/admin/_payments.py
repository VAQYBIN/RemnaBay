"""Платежи, «Требуют внимания» и карточка платежа (А4, А5; 4.20–4.23, 4.27, 4.30–4.32).

Разбирать зависший платёж и проваленные операции может любой участник команды
(01-domain, таблица ролей). Каждое действие — в журнале с участником (4.25).
Возврат денег — блок 10 (этап 9).
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, PositiveInt
from sqlalchemy import Select, select

from remnabay.attention import attention
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.journal import Actor, ActorType, JsonValue, Outcome, entries_for
from remnabay.payments import (
    PaymentActionError,
    TariffSnapshot,
    apply_as_new_subscription,
    bind_unknown_payment,
    payment_card,
    payment_subject,
    resolve_payment_manually,
    retry_payment,
)
from remnabay.queue import (
    AttemptResult,
    TaskNotCancellableError,
    TaskNotFailedError,
    attempts_of,
    cancel_failed,
    resolve_failed,
    retry_failed,
    waiting_panel_tasks,
)
from remnabay.shop_settings import shop_time_zone
from remnabay.web.admin._deps import DbSession, Member
from remnabay.worker.main import TASKS

router = APIRouter(tags=["payments"])

# Список платежей — последние, без бесконечной ленты
PAGE_SIZE = 100


class ActionRejectedOut(BaseModel):
    reason: Literal["not_available"] = "not_available"
    message: str


def _rejected(error: Exception) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, ActionRejectedOut(message=str(error)).model_dump()
    )


_REJECTED: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {"model": ActionRejectedOut}
}


# --- Модели ответов ---


class ClientRef(BaseModel):
    id: int
    telegram_id: int | None
    username: str | None
    name: str | None


class SubscriptionRef(BaseModel):
    id: int
    name: str
    panel_username: str


class PaymentRow(BaseModel):
    id: int
    state: PaymentState
    is_unknown: bool
    # Подтверждённая сумма не совпала со счётом (3.39)
    amount_mismatch: bool
    purpose: PaymentPurpose | None
    amount: Decimal
    currency: str
    tariff_name: str | None
    client: ClientRef | None
    created_at: datetime
    paid_at: datetime | None
    # Применение ждёт восстановления панели (4.30)
    waiting_panel: bool = False


class OperationRow(BaseModel):
    """Проваленная операция, которая не платёж (4.31)."""

    task_id: int
    name: str
    key: str | None
    failed_at: datetime | None
    last_error: str | None
    waiting_behind: int
    cancellable: bool


class WaitingRow(BaseModel):
    task_id: int
    name: str
    key: str | None
    created_at: datetime
    payment_id: int | None


class AttentionOut(BaseModel):
    payments: list[PaymentRow]
    operations: list[OperationRow]
    waiting_panel: list[WaitingRow]
    count: int
    # Даты в админке — в часовом поясе магазина (1.18)
    time_zone: str


class PaymentsOut(BaseModel):
    payments: list[PaymentRow]
    time_zone: str


class AttemptOut(BaseModel):
    number: int
    retry_round: int
    started_at: datetime
    finished_at: datetime | None
    result: AttemptResult | None
    error: str | None


class HistoryOut(BaseModel):
    occurred_at: datetime
    actor_type: ActorType
    actor_ref: str | None
    action: str
    outcome: Outcome
    details: dict[str, JsonValue]


class SnapshotOut(BaseModel):
    tariff_id: int
    name: str
    duration_days: int | None
    price: Decimal
    device_limit: int


class PaymentCardOut(BaseModel):
    payment: PaymentRow
    subscription: SubscriptionRef | None
    snapshot: SnapshotOut | None
    paid_amount: Decimal | None
    paid_currency: str | None
    provider: str | None
    provider_payment_id: str | None
    expires_at: datetime | None
    applied_at: datetime | None
    last_error: str | None
    attempts: list[AttemptOut]
    history: list[HistoryOut]
    # Действия, доступные в состоянии платежа (4.22, 4.23)
    actions: list[Literal["retry", "apply_as_new", "resolve", "bind"]]
    time_zone: str


# --- Сборка ---


async def _client_ref(session: DbSession, client_id: int | None) -> ClientRef | None:
    if client_id is None:
        return None
    account = await session.scalar(
        select(TelegramAccount)
        .where(TelegramAccount.client_id == client_id)
        .order_by(TelegramAccount.created_at)
        .limit(1)
    )
    if account is None:
        return ClientRef(id=client_id, telegram_id=None, username=None, name=None)
    name = " ".join(part for part in (account.first_name, account.last_name) if part) or None
    return ClientRef(
        id=client_id, telegram_id=account.telegram_id, username=account.username, name=name
    )


async def _row(session: DbSession, payment: Payment, waiting: set[int] | None = None) -> PaymentRow:
    snapshot = payment.tariff_snapshot or {}
    tariff_name = snapshot.get("name")
    return PaymentRow(
        id=payment.id,
        state=payment.state,
        is_unknown=payment.is_unknown,
        amount_mismatch=payment.amount_mismatch,
        purpose=payment.purpose,
        amount=payment.amount,
        currency=payment.currency,
        tariff_name=tariff_name if isinstance(tariff_name, str) else None,
        client=await _client_ref(session, payment.client_id),
        created_at=payment.created_at,
        paid_at=payment.paid_at,
        waiting_panel=payment.id in (waiting or set()),
    )


def _payment_id(args: dict[str, JsonValue]) -> int | None:
    value = args.get("payment_id")
    return value if isinstance(value, int) else None


# --- Список и «Требуют внимания» ---


@router.get("/attention")
async def attention_list(session: DbSession, member: Member) -> AttentionOut:
    """«Требуют внимания» (4.20, 4.23, 4.30, 4.31): проваленные платежи и операции,
    неизвестные платежи; отдельно — операции, которые ждут панель."""
    del member
    needs = await attention(session, TASKS)
    waiting = await waiting_panel_tasks(session)
    return AttentionOut(
        payments=[await _row(session, payment) for payment in needs.payments],
        operations=[
            OperationRow(
                task_id=failed.task_id,
                name=failed.name,
                key=failed.key,
                failed_at=failed.failed_at,
                last_error=failed.last_error,
                waiting_behind=failed.waiting_behind,
                cancellable=failed.cancellable,
            )
            for failed in needs.operations
        ],
        waiting_panel=[
            WaitingRow(
                task_id=task.task_id,
                name=task.name,
                key=task.key,
                created_at=task.created_at,
                payment_id=_payment_id(task.args),
            )
            for task in waiting
        ],
        count=needs.count,
        time_zone=(await shop_time_zone(session)).key,
    )


type _Filter = Literal[
    "pending",
    "paid",
    "applied",
    "declined",
    "expired",
    "cancelled",
    "paid_not_applied",
    "resolved_manually",
    "refunded",
    "partially_refunded",
    "unknown",
]


@router.get("/payments")
async def payments_list(
    session: DbSession,
    member: Member,
    state: Annotated[_Filter | None, Query()] = None,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
) -> PaymentsOut:
    """А4: платежи с фильтром по состоянию (отдельно — неизвестные) и периоду; даты
    периода — в часовом поясе магазина (1.18). Последние сто, от новых к старым."""
    del member
    query: Select[Payment] = select(Payment)
    if state == "unknown":
        query = query.where(Payment.is_unknown.is_(True))
    elif state is not None:
        query = query.where(Payment.state == PaymentState(state))
    zone = await shop_time_zone(session)
    if since is not None:
        query = query.where(Payment.created_at >= datetime.combine(since, time(), zone))
    if until is not None:
        end = datetime.combine(until, time(), zone) + timedelta(days=1)
        query = query.where(Payment.created_at < end)
    payments = await session.scalars(
        query.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(PAGE_SIZE)
    )
    waiting = {
        payment_id
        for task in await waiting_panel_tasks(session)
        if (payment_id := _payment_id(task.args)) is not None
    }
    return PaymentsOut(
        payments=[await _row(session, payment, waiting) for payment in payments],
        time_zone=zone.key,
    )


# --- Карточка платежа ---


def _actions(payment: Payment) -> list[Literal["retry", "apply_as_new", "resolve", "bind"]]:
    if payment.state != PaymentState.PAID_NOT_APPLIED:
        return []
    if payment.client_id is None:
        return ["bind", "resolve"]
    return ["retry", "apply_as_new", "resolve"]


async def _card(session: DbSession, payment_id: int) -> PaymentCardOut:
    try:
        card = await payment_card(session, payment_id)
    except PaymentActionError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    payment = card.payment
    snapshot = None
    if payment.tariff_snapshot:
        frozen = TariffSnapshot.of_payment(payment)
        snapshot = SnapshotOut(
            tariff_id=frozen.tariff_id,
            name=frozen.name,
            duration_days=frozen.duration_days,
            price=frozen.price,
            device_limit=frozen.device_limit,
        )
    waiting = {
        waiting_id
        for task in await waiting_panel_tasks(session)
        if (waiting_id := _payment_id(task.args)) is not None
    }
    subscription = card.subscription
    return PaymentCardOut(
        payment=await _row(session, payment, waiting),
        subscription=SubscriptionRef(
            id=subscription.id, name=subscription.name, panel_username=subscription.panel_username
        )
        if subscription is not None
        else None,
        snapshot=snapshot,
        paid_amount=payment.paid_amount,
        paid_currency=payment.paid_currency,
        provider=payment.provider,
        provider_payment_id=payment.provider_payment_id,
        expires_at=payment.expires_at,
        applied_at=payment.applied_at,
        last_error=card.last_error,
        attempts=[
            AttemptOut(
                number=attempt.number,
                retry_round=attempt.retry_round,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                result=attempt.result,
                error=attempt.error,
            )
            for attempt in card.attempts
        ],
        history=[
            HistoryOut(
                occurred_at=entry.occurred_at,
                actor_type=entry.actor_type,
                actor_ref=entry.actor_ref,
                action=entry.action,
                outcome=entry.outcome,
                details=entry.details,
            )
            for entry in await entries_for(session, payment_subject(payment.id))
        ],
        actions=_actions(payment),
        time_zone=(await shop_time_zone(session)).key,
    )


@router.get("/payments/{payment_id}", responses={status.HTTP_404_NOT_FOUND: {}})
async def payment_details(payment_id: int, session: DbSession, member: Member) -> PaymentCardOut:
    """А5: клиент, что должно было примениться, история попыток и текст ошибки (4.21)."""
    del member
    return await _card(session, payment_id)


class CommentIn(BaseModel):
    comment: Annotated[str, Field(min_length=1, max_length=2000)]


@router.post("/payments/{payment_id}/retry", responses=_REJECTED)
async def retry(payment_id: int, session: DbSession, member: Member) -> PaymentCardOut:
    """«Применить повторно» (4.22)."""
    try:
        await retry_payment(session, payment_id, member_id=member.id)
    except PaymentActionError as error:
        raise _rejected(error) from error
    await session.commit()
    return await _card(session, payment_id)


@router.post("/payments/{payment_id}/apply-as-new", responses=_REJECTED)
async def apply_as_new(payment_id: int, session: DbSession, member: Member) -> PaymentCardOut:
    """«Применить созданием новой подписки» (4.22) — например, если пользователя удалили."""
    try:
        await apply_as_new_subscription(session, payment_id, member_id=member.id)
    except PaymentActionError as error:
        raise _rejected(error) from error
    await session.commit()
    return await _card(session, payment_id)


@router.post("/payments/{payment_id}/resolve", responses=_REJECTED)
async def resolve(
    payment_id: int, body: CommentIn, session: DbSession, member: Member
) -> PaymentCardOut:
    """«Отметить решённым вручную» с обязательным комментарием (4.22)."""
    try:
        await resolve_payment_manually(
            session, payment_id, member_id=member.id, comment=body.comment
        )
    except PaymentActionError as error:
        raise _rejected(error) from error
    await session.commit()
    return await _card(session, payment_id)


class BindIn(BaseModel):
    telegram_id: PositiveInt
    purpose: Literal["purchase", "renewal"]
    tariff_id: int
    subscription_id: int | None = None


@router.post("/payments/{payment_id}/bind", responses=_REJECTED)
async def bind(payment_id: int, body: BindIn, session: DbSession, member: Member) -> PaymentCardOut:
    """«Привязать к клиенту» неизвестный платёж и выбрать, что применить (4.23):
    покупку или продление подписки клиента выбранным тарифом."""
    account = await session.get(TelegramAccount, body.telegram_id)
    tariff = await session.get(Tariff, body.tariff_id)
    try:
        if account is None:
            raise PaymentActionError("Клиента с таким Telegram ID нет в магазине")
        if tariff is None:
            raise PaymentActionError("Тариф не найден")
        await bind_unknown_payment(
            session,
            payment_id,
            member_id=member.id,
            client_id=account.client_id,
            purpose=PaymentPurpose(body.purpose),
            tariff_id=tariff.id,
            tariff_snapshot=TariffSnapshot.of(tariff).stored(),
            subscription_id=body.subscription_id,
        )
    except PaymentActionError as error:
        raise _rejected(error) from error
    await session.commit()
    return await _card(session, payment_id)


class TariffOptionOut(BaseModel):
    id: int
    name: str
    duration_days: int | None
    price: Decimal
    state: TariffState


@router.get("/tariff-options")
async def tariff_options(session: DbSession, member: Member) -> list[TariffOptionOut]:
    """Тарифы для привязки неизвестного платежа (4.23): в продаже и в архиве."""
    del member
    tariffs = await session.scalars(
        select(Tariff)
        .where(Tariff.state != TariffState.CLOSED)
        .order_by(Tariff.state != TariffState.ON_SALE, Tariff.sort_order, Tariff.id)
    )
    return [
        TariffOptionOut(
            id=t.id, name=t.name, duration_days=t.duration_days, price=t.price, state=t.state
        )
        for t in tariffs
    ]


class ClientLookupOut(BaseModel):
    client: ClientRef
    subscriptions: list[SubscriptionRef]


@router.get("/clients/by-telegram/{telegram_id}", responses={status.HTTP_404_NOT_FOUND: {}})
async def client_by_telegram(
    telegram_id: int, session: DbSession, member: Member
) -> ClientLookupOut:
    """Клиент по Telegram ID и его подписки — для привязки неизвестного платежа (4.23)."""
    del member
    account = await session.get(TelegramAccount, telegram_id)
    client = await session.get(Client, account.client_id) if account is not None else None
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Клиента с таким Telegram ID нет")
    subscriptions = await session.scalars(
        select(Subscription)
        .where(Subscription.client_id == client.id, Subscription.deleted_at.is_(None))
        .order_by(Subscription.id)
    )
    return ClientLookupOut(
        client=ClientRef(
            id=client.id,
            telegram_id=account.telegram_id if account is not None else None,
            username=account.username if account is not None else None,
            name=" ".join(p for p in (account.first_name, account.last_name) if p) or None
            if account is not None
            else None,
        ),
        subscriptions=[
            SubscriptionRef(id=s.id, name=s.name, panel_username=s.panel_username)
            for s in subscriptions
        ],
    )


# --- Проваленные операции (4.31, 4.32) ---


class OperationAttemptsOut(BaseModel):
    task_id: int
    attempts: list[AttemptOut]


@router.get("/operations/{task_id}/attempts")
async def operation_attempts(
    task_id: int, session: DbSession, member: Member
) -> OperationAttemptsOut:
    """История попыток операции с текстом ошибок (4.18)."""
    del member
    return OperationAttemptsOut(
        task_id=task_id,
        attempts=[
            AttemptOut(
                number=attempt.number,
                retry_round=attempt.retry_round,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                result=attempt.result,
                error=attempt.error,
            )
            for attempt in await attempts_of(session, task_id)
        ],
    )


@router.post("/operations/{task_id}/retry", responses=_REJECTED)
async def retry_operation(task_id: int, session: DbSession, member: Member) -> AttentionOut:
    """«Повторить» (4.31)."""
    try:
        await retry_failed(session, task_id, actor=Actor.team_member(member.id))
    except TaskNotFailedError as error:
        raise _rejected(error) from error
    await session.commit()
    return await attention_list(session, member)


@router.post("/operations/{task_id}/cancel", responses=_REJECTED)
async def cancel_operation(
    task_id: int, body: CommentIn, session: DbSession, member: Member
) -> AttentionOut:
    """«Отменить» с комментарием (4.31, 4.32): следующие операции подписки идут дальше,
    действие считается невыполненным. Для смены даты при возврате — недоступна."""
    try:
        await cancel_failed(
            session, TASKS, task_id, actor=Actor.team_member(member.id), comment=body.comment
        )
    except (TaskNotFailedError, TaskNotCancellableError, ValueError) as error:
        raise _rejected(error) from error
    await session.commit()
    return await attention_list(session, member)


@router.post("/operations/{task_id}/resolve", responses=_REJECTED)
async def resolve_operation(
    task_id: int, body: CommentIn, session: DbSession, member: Member
) -> AttentionOut:
    """«Отметить решённым вручную» с комментарием (4.31)."""
    try:
        await resolve_failed(
            session, TASKS, task_id, actor=Actor.team_member(member.id), comment=body.comment
        )
    except (TaskNotFailedError, ValueError) as error:
        raise _rejected(error) from error
    await session.commit()
    return await attention_list(session, member)
