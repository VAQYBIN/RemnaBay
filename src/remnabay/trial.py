"""Триал: настройки, доступность и выдача (блок 5, решения 0013, 0035).

- Триал доступен клиенту, если оператор его включил, а клиент ещё не получал
  триал, ни разу не платил, не усыновлён и подписок у него нет (5.2, 5.3).
- «Попробовать бесплатно» ставит операцию очереди: создание пользователя в панели
  повторяется, пока панель недоступна (4.14). Если за 10 секунд подписка не
  создана, клиент получает «Создаём пробную подписку» (5.6, С18).
- Триал считается выданным, когда пользователь панели создан: тогда же
  появляется факт использования (5.6). Повторное нажатие второго триала не создаёт
  (5.5): пока создание ждёт в очереди, новое не ставится.
- Параметры триала фиксируются в момент нажатия: изменение настроек, пока панель
  недоступна, уже выданное не меняет.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment
from remnabay.domain.trials import Trial
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import (
    MAIN_MENU_BUTTON,
    ButtonArgs,
    guide_button,
    load_texts,
    send_to_client,
    tell_action_failed,
)
from remnabay.panel_users import Access, NewUserError, new_subscription, panel_user
from remnabay.queue import TaskContext, task, unfinished_keys
from remnabay.shop import TRIAL_ENABLED
from remnabay.shop_settings import ShopSetting, get_setting, set_setting

SUBSCRIPTION_SUBJECT = "subscription"
CLIENT_SUBJECT = "client"
# Гигабайт трафика — как в панели: 1024³ байт
GIGABYTE = 1024**3
# Если за это время триал не создан, клиент получает «Создаём пробную подписку» (5.6):
# как у оплаты (4.19)
PROCESSING_NOTICE_DELAY = timedelta(seconds=10)
MAX_DURATION_DAYS = 365
MAX_TRAFFIC_GB = 100_000
MAX_DEVICES = 100

# «Триал» (5.1, 04-operator-settings): срок 3 дня, 10 ГБ, одно устройство, сквады не выбраны
TRIAL_DURATION_DAYS = ShopSetting(
    "trial.duration_days",
    TypeAdapter[int](Annotated[int, Field(ge=1, le=MAX_DURATION_DAYS)]),
    3,
)
TRIAL_TRAFFIC_GB = ShopSetting(
    "trial.traffic_limit_gb",
    TypeAdapter[int](Annotated[int, Field(ge=0, le=MAX_TRAFFIC_GB)]),
    10,
)
TRIAL_DEVICE_LIMIT = ShopSetting(
    "trial.device_limit",
    TypeAdapter[int](Annotated[int, Field(ge=1, le=MAX_DEVICES)]),
    1,
)
TRIAL_SQUADS = ShopSetting("trial.squads", TypeAdapter(list[UUID]), [])


class TrialParams(BaseModel):
    """Параметры триала (5.1). Лимит трафика 0 — без ограничения; трафик не сбрасывается."""

    duration_days: int
    traffic_limit_gb: int
    device_limit: int
    squad_uuids: list[UUID]


class TrialSettingsError(Exception):
    """Триал нельзя включить с такими параметрами; текст показывается оператору."""


async def trial_settings(session: AsyncSession) -> tuple[bool, TrialParams]:
    """Включён ли триал и его параметры."""
    return await get_setting(session, TRIAL_ENABLED), TrialParams(
        duration_days=await get_setting(session, TRIAL_DURATION_DAYS),
        traffic_limit_gb=await get_setting(session, TRIAL_TRAFFIC_GB),
        device_limit=await get_setting(session, TRIAL_DEVICE_LIMIT),
        squad_uuids=await get_setting(session, TRIAL_SQUADS),
    )


async def save_trial_settings(
    session: AsyncSession, enabled: bool, params: TrialParams, *, member_id: int
) -> None:
    """5.1: оператор включает или выключает триал и задаёт параметры. Сквады
    обязательно выбрать при включении (04-operator-settings). Каждое изменение — в журнал."""
    if enabled and not params.squad_uuids:
        raise TrialSettingsError("Выберите хотя бы один сквад — иначе триал не включить")
    changes = (
        (TRIAL_DURATION_DAYS, params.duration_days),
        (TRIAL_TRAFFIC_GB, params.traffic_limit_gb),
        (TRIAL_DEVICE_LIMIT, params.device_limit),
    )
    for setting, value in changes:
        if await get_setting(session, setting) != value:
            await set_setting(session, setting, value, member_id=member_id)
    squads = list(dict.fromkeys(params.squad_uuids))
    if await get_setting(session, TRIAL_SQUADS) != squads:
        await set_setting(session, TRIAL_SQUADS, squads, member_id=member_id)
    if await get_setting(session, TRIAL_ENABLED) != enabled:
        await set_setting(session, TRIAL_ENABLED, enabled, member_id=member_id)


def _trial_key(client_id: int) -> str:
    """Ключ очереди создания триала: одно создание на клиента за раз (5.5)."""
    return f"client:{client_id}:trial"


async def _creating(session: AsyncSession, client_id: int) -> bool:
    """Создание триала уже ждёт в очереди (или провалилось и ждёт команду)."""
    key = _trial_key(client_id)
    return key in await unfinished_keys(session, create_trial.name, [key])


async def _subscribed_or_paid(session: AsyncSession, client_id: int) -> bool:
    """Клиент хотя бы раз платил или у него есть подписка — триал не предлагается (5.3)."""
    paid = await session.scalar(
        select(exists().where(Payment.client_id == client_id, Payment.paid_at.is_not(None)))
    )
    subscribed = await session.scalar(
        select(
            exists().where(Subscription.client_id == client_id, Subscription.deleted_at.is_(None))
        )
    )
    return bool(paid or subscribed)


async def trial_available(session: AsyncSession, client: Client) -> bool:
    """5.2, 5.3: триал включён, клиент его ещё не получал (и не ждёт), ни разу не
    платил, не усыновлён и подписок у него нет."""
    if not await get_setting(session, TRIAL_ENABLED) or client.adopted_at is not None:
        return False
    used = await session.scalar(select(exists().where(Trial.client_id == client.id)))
    if used or await _subscribed_or_paid(session, client.id):
        return False
    return not await _creating(session, client.id)


class TrialArgs(BaseModel):
    client_id: int
    # Параметры на момент нажатия
    params: TrialParams


async def request_trial(session: AsyncSession, client: Client) -> bool:
    """«Попробовать бесплатно» (5.4): ставит создание триала. `False` — триал
    недоступен (в том числе уже создаётся по прошлому нажатию, 5.5).

    Строка клиента блокируется: два одновременных нажатия не поставят два создания.
    """
    await session.refresh(client, with_for_update=True)
    if not await trial_available(session, client):
        return False
    _enabled, params = await trial_settings(session)
    if not params.squad_uuids:
        return False
    args = TrialArgs(client_id=client.id, params=params)
    await create_trial.enqueue(session, args, key=_trial_key(client.id))
    await trial_processing_notice.enqueue(
        session, TrialNoticeArgs(client_id=client.id), delay=PROCESSING_NOTICE_DELAY
    )
    await record(
        session,
        actor=Actor.client(client.id),
        action="trial.requested",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, client.id),
        details=params.model_dump(mode="json"),
    )
    return True


@task("trials.create", TrialArgs)
async def create_trial(context: TaskContext, args: TrialArgs) -> None:
    """Создаёт триальную подписку: пользователь панели с параметрами триала, отрезок
    нулевой стоимости, факт использования (5.4, 5.6). Уже выданный — ничего не делает
    второй раз (сквозное правило 3)."""
    session = context.session
    client = await session.get(Client, args.client_id, with_for_update=True)
    if client is None or await session.get(Trial, args.client_id) is not None:
        return
    if await _subscribed_or_paid(session, client.id):
        # Пока создание ждало панель, клиент купил подписку: триал ему больше не
        # положен (5.3), а вторая подписка не нужна
        await record(
            session,
            actor=Actor.SYSTEM,
            action="trial.skipped",
            outcome=Outcome.SUCCESS,
            subject=Subject(CLIENT_SUBJECT, client.id),
            details={"reason": "subscribed_or_paid"},
        )
        return
    params = args.params
    access = Access(
        expire_at=datetime.now(UTC) + timedelta(days=params.duration_days),
        traffic_limit_bytes=params.traffic_limit_gb * GIGABYTE,
        device_limit=params.device_limit,
        squad_uuids=params.squad_uuids,
    )
    try:
        user = await panel_user(session, client.id, access, dev_mode=runtime.current().dev_mode)
    except NewUserError as error:
        # Повторы, затем «Требуют внимания» (4.14, 4.31)
        raise RuntimeError(str(error)) from error
    subscription = await new_subscription(session, client.id, user, tariff_id=None, is_trial=True)
    days = timedelta(days=params.duration_days)
    session.add(
        TermSegment(
            subscription_id=subscription.id,
            kind=SegmentKind.TRIAL,
            starts_at=user.expire_at - days,
            ends_at=user.expire_at,
        )
    )
    session.add(Trial(client_id=client.id, subscription_id=subscription.id))
    await session.flush()
    await record(
        session,
        actor=Actor.SYSTEM,
        action="subscription.created",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
        details={"trial": True, "expires_at": user.expire_at.isoformat()},
    )
    await record(
        session,
        actor=Actor.SYSTEM,
        action="trial.issued",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, client.id),
        details={"subscription_id": subscription.id},
    )
    await _tell_activated(session, client, subscription, params)


async def _tell_activated(
    session: AsyncSession, client: Client, subscription: Subscription, params: TrialParams
) -> None:
    """С3: ссылка и срок; «Скопировать ссылку», «Как подключить», «Главное меню»."""
    texts = await load_texts(session)
    period = texts.quantity("unit.days", client.language_code, params.duration_days)
    link = subscription.subscription_url
    await send_to_client(
        session,
        client.id,
        "event.trial_activated",
        variables={"trial_period": period, "subscription_link": link, "end_date": ""},
        dates={"end_date": subscription.expires_at} if subscription.expires_at else {},
        buttons=[
            ButtonArgs(text_key="btn.copy_link", copy_text=link),
            guide_button(subscription.id),
            MAIN_MENU_BUTTON,
        ],
    )


@create_trial.on_cancelled
async def _trial_cancelled(session: AsyncSession, _task_id: int, args: TrialArgs) -> None:
    """4.32: команда отменила создание — триал не считается использованным, клиент
    получает «Не удалось выполнить действие»."""
    await tell_action_failed(session, args.client_id)


class TrialNoticeArgs(BaseModel):
    client_id: int


@task("trials.processing_notice", TrialNoticeArgs, needs_attention=False)
async def trial_processing_notice(context: TaskContext, args: TrialNoticeArgs) -> None:
    """5.6, С18: триал не создан сразу (панель недоступна) — «Создаём пробную подписку»."""
    session = context.session
    if await session.get(Trial, args.client_id) is not None:
        return
    if not await _creating(session, args.client_id):
        # Создание отменено командой — клиент уже получил «Не удалось выполнить действие»
        return
    await send_to_client(
        session, args.client_id, "event.trial_processing", buttons=[MAIN_MENU_BUTTON]
    )
