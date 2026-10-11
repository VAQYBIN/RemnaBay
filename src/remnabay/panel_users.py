"""Новая подписка в панели: пользователь и строка подписки магазина (3.6, 3.14, 5.4).

Общее для покупки и триала. Имя пользователя — `<префикс>_<Telegram ID>_<N>`
(решение 0057). Перед созданием магазин проверяет, не создала ли его прошлая
попытка, которая не успела записать подписку (4.15): тогда берёт его же, второго
не создаёт.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.clients import client_telegram_id
from remnabay.domain.clients import Client
from remnabay.domain.subscriptions import Subscription
from remnabay.messaging import load_texts
from remnabay.panel import CreateUser, TrafficStrategy, User
from remnabay.panel_names import lock_prefix, panel_username
from remnabay.panel_sync import accept_access

# Сколько занятых чужими пользователями имён пропустить, прежде чем сдаться
_MAX_NAME_TRIES = 50
_NAME_LIMIT = 64


class NewUserError(Exception):
    """Пользователя панели для новой подписки создать нельзя."""


@dataclass(frozen=True)
class Access:
    """Параметры доступа нового пользователя панели. Трафик не сбрасывается."""

    expire_at: datetime
    # 0 — без ограничения
    traffic_limit_bytes: int
    device_limit: int
    squad_uuids: list[UUID]


async def _next_number(session: AsyncSession, client_id: int, prefix: str, telegram_id: int) -> int:
    """Следующий порядковый номер подписки клиента, созданной магазином."""
    start = f"{prefix}_{telegram_id}_"
    names = await session.scalars(
        select(Subscription.panel_username).where(Subscription.client_id == client_id)
    )
    numbers = [
        int(name.removeprefix(start))
        for name in names
        if name.startswith(start) and name.removeprefix(start).isdigit()
    ]
    return max(numbers, default=0) + 1


async def _bound(session: AsyncSession, panel_user_id: int) -> bool:
    return (
        await session.scalar(
            select(Subscription.id).where(Subscription.panel_user_id == panel_user_id)
        )
        is not None
    )


async def panel_user(
    session: AsyncSession, client_id: int, access: Access, *, dev_mode: bool
) -> User:
    """Пользователь панели для новой подписки. Если прошлая попытка его уже создала,
    а записать подписку не успела, — он же, второго не создаём (4.15)."""
    telegram_id = await client_telegram_id(session, client_id)
    if telegram_id is None:
        raise NewUserError("У клиента нет Telegram-аккаунта")
    panel = runtime.current().panel
    prefix = await lock_prefix(session, dev_mode=dev_mode)
    number = await _next_number(session, client_id, prefix, telegram_id)
    for _ in range(_MAX_NAME_TRIES):
        username = panel_username(prefix, telegram_id, number)
        existing = await panel.get_user_by_username(username)
        if existing is None:
            return await panel.create_user(
                CreateUser(
                    username=username,
                    expire_at=access.expire_at,
                    traffic_limit_bytes=access.traffic_limit_bytes,
                    traffic_limit_strategy=TrafficStrategy.NO_RESET,
                    hwid_device_limit=access.device_limit,
                    active_internal_squads=access.squad_uuids,
                    telegram_id=telegram_id,
                )
            )
        if existing.telegram_id == telegram_id and not await _bound(session, existing.id):
            return existing
        # Имя занято чужим пользователем (создан в панели вручную) — следующий номер
        number += 1
    raise NewUserError("Не удалось подобрать свободное имя пользователя в панели")


async def default_name(session: AsyncSession, client_id: int) -> str:
    """Название первой подписки на языке клиента (3.14, решение 0057)."""
    language = await session.scalar(select(Client.language_code).where(Client.id == client_id))
    texts = await load_texts(session)
    name = texts.render("subscription.default_name", language).strip()
    return name[:_NAME_LIMIT] or texts.render("subscription.default_name", None)[:_NAME_LIMIT]


async def new_subscription(
    session: AsyncSession,
    client_id: int,
    user: User,
    *,
    tariff_id: int | None,
    is_trial: bool = False,
) -> Subscription:
    """Строка подписки магазина для только что созданного пользователя панели."""
    subscription = Subscription(
        client_id=client_id,
        name=await default_name(session, client_id),
        tariff_id=tariff_id,
        is_trial=is_trial,
        panel_user_id=user.id,
        panel_username=user.username,
        panel_short_uuid=user.short_uuid,
        subscription_url=user.subscription_url,
    )
    session.add(subscription)
    await session.flush()
    accept_access(subscription, user)
    return subscription
