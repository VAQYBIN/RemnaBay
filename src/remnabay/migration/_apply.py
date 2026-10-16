"""Применение плана: клиенты, подписки, данные файла, реферальные связи (9.3–9.7, 9.10–9.17).

Каждый шаг идемпотентен: уже привязанный пользователь панели пропускается (9.7), а
клиенту, уже перенесённому из файла, данные файла второй раз не записываются.
В панели ничего не меняется (9.6): подписка строится из снимка панели.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import Tariff
from remnabay.domain.trials import Trial
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.migration._plan import (
    BoundUser,
    ClientPlan,
    InviteLink,
    KnownClient,
    ShopState,
    TariffParams,
)
from remnabay.panel import User
from remnabay.panel_users import new_subscription, numbered_name
from remnabay.texts import Texts

CLIENT_SUBJECT = "client"
SUBSCRIPTION_SUBJECT = "subscription"


async def shop_state(session: AsyncSession) -> ShopState:
    """Что уже есть в магазине: клиенты по Telegram ID, привязанные пользователи
    панели, реферальные коды, тарифы."""
    subscriptions: dict[int, int] = defaultdict(int)
    for client_id, count in await session.execute(
        select(Subscription.client_id, func.count())
        .where(Subscription.deleted_at.is_(None))
        .group_by(Subscription.client_id)
    ):
        subscriptions[client_id] = count
    trials = set(await session.scalars(select(Trial.client_id)))
    invited = set(await session.scalars(select(ReferralLink.invited_id)))
    telegram_of: dict[int, int] = {}
    clients: dict[int, KnownClient] = {}
    for telegram_id, client_id, adopted_at, imported_at in await session.execute(
        select(TelegramAccount.telegram_id, Client.id, Client.adopted_at, Client.imported_at).join(
            Client, Client.id == TelegramAccount.client_id
        )
    ):
        telegram_of.setdefault(client_id, telegram_id)
        clients[telegram_id] = KnownClient(
            client_id=client_id,
            adopted=adopted_at is not None,
            imported=imported_at is not None,
            subscriptions=subscriptions[client_id],
            has_trial=client_id in trials,
            has_inviter=client_id in invited,
        )
    bound = {
        panel_user_id: BoundUser(
            telegram_id=telegram_of.get(client_id), adopted=adopted_at is not None
        )
        for panel_user_id, client_id, adopted_at in await session.execute(
            select(Subscription.panel_user_id, Subscription.client_id, Subscription.adopted_at)
        )
    }
    codes = {
        code: client_id
        for code, client_id in await session.execute(
            select(ReferralCode.code, ReferralCode.client_id)
        )
    }
    tariffs = {
        tariff.id: TariffParams(
            squads=frozenset(tariff.squad_uuids),
            device_limit=tariff.device_limit,
            traffic_limit_bytes=tariff.traffic_limit_bytes,
            traffic_reset_strategy=tariff.traffic_reset_strategy,
        )
        for tariff in await session.scalars(select(Tariff))
    }
    return ShopState(clients=clients, bound=bound, codes=codes, tariffs=tariffs)


@dataclass(frozen=True)
class Context:
    run_id: int
    actor: Actor
    texts: Texts
    now: datetime


async def _client(session: AsyncSession, item: ClientPlan, context: Context) -> tuple[Client, bool]:
    """Клиент по Telegram ID: найденный или новый. Новый — с данными файла (9.17)."""
    account = await session.get(TelegramAccount, item.telegram_id)
    if account is not None:
        return await session.get_one(Client, account.client_id), False
    data = item.file
    client = Client(
        first_seen_at=item.first_seen_at,
        language_code=data.language if data is not None else None,
    )
    session.add(client)
    await session.flush()
    session.add(
        TelegramAccount(
            telegram_id=item.telegram_id,
            client_id=client.id,
            username=data.username if data is not None else None,
            first_name=data.name if data is not None else None,
        )
    )
    await session.flush()
    return client, True


async def _fill(session: AsyncSession, client: Client, item: ClientPlan, now: datetime) -> bool:
    """Данные файла — один раз: клиента, уже перенесённого из файла, импорт не меняет
    (9.7, решения 0070, 0071)."""
    data = item.file
    if data is None or client.imported_at is not None:
        return False
    client.imported_at = now
    client.old_bot_paid = data.paid
    if data.first_seen_at is not None and (
        client.first_seen_at is None or data.first_seen_at < client.first_seen_at
    ):
        client.first_seen_at = data.first_seen_at
    if data.trial_used and await session.get(Trial, client.id) is None:
        # Триал из старого бота: новый магазин его не выдаст (9.17)
        session.add(Trial(client_id=client.id, imported=True))
    if data.codes:
        await session.execute(
            insert(ReferralCode)
            .values(
                [{"code": code, "client_id": client.id, "migrated": True} for code in data.codes]
            )
            .on_conflict_do_nothing()
        )
    await session.flush()
    return True


async def _adopt(
    session: AsyncSession,
    client: Client,
    item: ClientPlan,
    users: dict[int, User],
    context: Context,
) -> tuple[list[int], int]:
    """Подписки из пользователей панели (9.4, 9.5, 9.15, 9.16). Возвращает подписки и
    сколько пропущено: пользователя успели привязать или он пропал из снимка."""
    adopted: list[int] = []
    skipped = 0
    for planned in item.subscriptions:
        user = users.get(planned.panel_user_id)
        taken = await session.scalar(
            select(Subscription.id).where(Subscription.panel_user_id == planned.panel_user_id)
        )
        if user is None or taken is not None:
            skipped += 1
            continue
        tariff_id = planned.tariff_id
        if tariff_id is not None and await session.get(Tariff, tariff_id) is None:
            tariff_id = None
        subscription = await new_subscription(
            session,
            client.id,
            user,
            tariff_id=tariff_id,
            name=numbered_name(context.texts, client.language_code, planned.number),
        )
        subscription.adopted_at = context.now
        if user.expire_at > context.now:
            # Перенесённые дни — отрезок нулевой стоимости (9.5, решение 0012)
            session.add(
                TermSegment(
                    subscription_id=subscription.id,
                    kind=SegmentKind.MIGRATED,
                    starts_at=context.now,
                    ends_at=user.expire_at,
                )
            )
        await session.flush()
        await record(
            session,
            actor=context.actor,
            action="subscription.adopted",
            outcome=Outcome.SUCCESS,
            subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
            details={
                "run_id": context.run_id,
                "client_id": client.id,
                "panel_user_id": user.id,
                "tariff_id": tariff_id,
            },
        )
        adopted.append(subscription.id)
    return adopted, skipped


async def apply_client(
    session: AsyncSession, item: ClientPlan, users: dict[int, User], context: Context
) -> int:
    """Применяет план одного клиента. Возвращает, сколько пропущено (9.7)."""
    client, created = await _client(session, item, context)
    filled = await _fill(session, client, item, context.now)
    skipped = int(item.file is not None and not filled)
    adopted, not_adopted = await _adopt(session, client, item, users, context)
    skipped += not_adopted
    if adopted and client.adopted_at is None:
        client.adopted_at = context.now
    if not adopted and not filled:
        return skipped
    await session.flush()
    await record(
        session,
        actor=context.actor,
        action="client.adopted" if adopted else "client.imported",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, client.id),
        details={
            "run_id": context.run_id,
            "telegram_id": item.telegram_id,
            "new_client": created,
            "subscriptions": list[JsonValue](adopted),
            "from_file": filled,
        },
    )
    return skipped


async def apply_link(session: AsyncSession, link: InviteLink, context: Context) -> bool:
    """Реферальная связь из файла (9.11); существующую не меняет (3.15, 0071)."""
    invited = await session.get(TelegramAccount, link.invited)
    inviter = await session.get(TelegramAccount, link.inviter)
    if invited is None or inviter is None or invited.client_id == inviter.client_id:
        return False
    if await session.get(ReferralLink, invited.client_id) is not None:
        return False
    session.add(
        ReferralLink(invited_id=invited.client_id, inviter_id=inviter.client_id, migrated=True)
    )
    await session.flush()
    await record(
        session,
        actor=context.actor,
        action="referral.linked",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, invited.client_id),
        details={"inviter_id": inviter.client_id, "migrated": True, "run_id": context.run_id},
    )
    return True
