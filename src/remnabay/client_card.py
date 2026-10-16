"""Поиск клиента и его карточка в админке (А2, А3; 10.1, 10.2).

Карточка — то, что магазин знает о клиенте: подписки с последним известным
состоянием из панели, платежи, триал, реферальные связи, запросы на возврат и
журнал. Устройства магазин не хранит — их показывает панель (блок 6).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, RefundRequest
from remnabay.domain.referrals import ReferralLink
from remnabay.domain.subscriptions import Subscription, operations_key
from remnabay.domain.trials import Trial
from remnabay.journal import JournalEntry
from remnabay.queue import unfinished_counts

# Строк в списке и в разделах карточки: последние, без бесконечной ленты
LIST_LIMIT = 50
CARD_LIMIT = 100
# Telegram ID — целое до 2^63: длиннее строка цифр — не Telegram ID
_MAX_ID_DIGITS = 18


def _contains(text: str) -> str:
    """Шаблон ILIKE «содержит» с экранированными `%`, `_` и `\\`."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _short_uuid(text: str) -> str:
    """Короткий идентификатор из ссылки подписки: последний сегмент пути."""
    path = urlsplit(text).path if "://" in text else text
    return path.rstrip("/").rsplit("/", 1)[-1]


def _matches(query: str) -> ColumnElement[bool]:
    """10.1: Telegram ID, username, имя, ссылка подписки, имя пользователя в панели."""
    text = query.removeprefix("@")
    pattern = _contains(text)
    name = func.concat_ws(" ", TelegramAccount.first_name, TelegramAccount.last_name)
    by_subscription = exists().where(
        Subscription.client_id == Client.id,
        or_(
            Subscription.panel_username.ilike(pattern, escape="\\"),
            Subscription.subscription_url == query,
            Subscription.panel_short_uuid == _short_uuid(query),
        ),
    )
    conditions: list[ColumnElement[bool]] = [
        TelegramAccount.username.ilike(pattern, escape="\\"),
        name.ilike(pattern, escape="\\"),
        by_subscription,
    ]
    if text.isdigit() and len(text) <= _MAX_ID_DIGITS:
        conditions.append(TelegramAccount.telegram_id == int(text))
    return or_(*conditions)


async def search_clients(session: AsyncSession, query: str) -> list[Client]:
    """Клиенты по строке поиска (10.1); пустая строка — последние появившиеся."""
    query = query.strip()
    statement = select(Client).outerjoin(TelegramAccount, TelegramAccount.client_id == Client.id)
    if query:
        statement = statement.where(_matches(query))
    rows = await session.scalars(statement.distinct().order_by(Client.id.desc()).limit(LIST_LIMIT))
    return list(rows)


async def subscriptions_of(
    session: AsyncSession, client_ids: Sequence[int]
) -> dict[int, list[Subscription]]:
    """Подписки клиентов, включая удалённые в панели, — по клиенту."""
    found: dict[int, list[Subscription]] = {client_id: [] for client_id in client_ids}
    rows = await session.scalars(
        select(Subscription).where(Subscription.client_id.in_(client_ids)).order_by(Subscription.id)
    )
    for subscription in rows:
        found[subscription.client_id].append(subscription)
    return found


def nearest_end(subscriptions: Sequence[Subscription], now: datetime) -> datetime | None:
    """Ближайшая дата окончания (А2): ближайшая впереди, иначе — последняя прошедшая."""
    ends = [s.expires_at for s in subscriptions if s.deleted_at is None and s.expires_at]
    ahead = [end for end in ends if end >= now]
    if ahead:
        return min(ahead)
    return max(ends) if ends else None


@dataclass(frozen=True)
class Referrals:
    inviter_id: int | None
    invited_ids: list[int]


async def referrals(session: AsyncSession, client_id: int) -> Referrals:
    """Кто пригласил клиента и кого пригласил он (10.2)."""
    inviter_id = await session.scalar(
        select(ReferralLink.inviter_id).where(ReferralLink.invited_id == client_id)
    )
    invited = await session.scalars(
        select(ReferralLink.invited_id)
        .where(ReferralLink.inviter_id == client_id)
        .order_by(ReferralLink.created_at.desc())
        .limit(CARD_LIMIT)
    )
    return Referrals(inviter_id=inviter_id, invited_ids=list(invited))


async def payments_of(session: AsyncSession, client_id: int) -> list[Payment]:
    """Платежи клиента, от новых к старым (10.2)."""
    rows = await session.scalars(
        select(Payment)
        .where(Payment.client_id == client_id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(CARD_LIMIT)
    )
    return list(rows)


async def trial_of(session: AsyncSession, client_id: int) -> Trial | None:
    return await session.get(Trial, client_id)


async def refund_requests_of(session: AsyncSession, client_id: int) -> list[RefundRequest]:
    """Запросы на возврат: сначала открытые, затем закрытые от новых к старым (10.6)."""
    rows = await session.scalars(
        select(RefundRequest)
        .where(RefundRequest.client_id == client_id)
        .order_by(RefundRequest.closed_at.desc().nulls_first(), RefundRequest.id.desc())
        .limit(CARD_LIMIT)
    )
    return list(rows)


async def journal_of(
    session: AsyncSession,
    client_id: int,
    subscription_ids: Sequence[int],
    payment_ids: Sequence[int],
) -> list[JournalEntry]:
    """Журнал по клиенту (10.2): записи о нём самом, его подписках и платежах — от
    новых к старым."""
    rows = await session.scalars(
        select(JournalEntry)
        .where(
            or_(
                and_(
                    JournalEntry.subject_type == "client",
                    JournalEntry.subject_id == str(client_id),
                ),
                and_(
                    JournalEntry.subject_type == "subscription",
                    JournalEntry.subject_id.in_([str(i) for i in subscription_ids]),
                ),
                and_(
                    JournalEntry.subject_type == "payment",
                    JournalEntry.subject_id.in_([str(i) for i in payment_ids]),
                ),
            )
        )
        .order_by(JournalEntry.id.desc())
        .limit(CARD_LIMIT)
    )
    return list(rows)


async def changes_waiting(
    session: AsyncSession, subscription_ids: Sequence[int], names: Sequence[str]
) -> dict[int, int]:
    """Сколько изменений каждой подписки (виды задач `names`) ещё не выполнено:
    например, выданные дни ждут панель (4.14, 4.30)."""
    keys = {
        operations_key(subscription_id): subscription_id for subscription_id in subscription_ids
    }
    counts = await unfinished_counts(session, keys, names)
    return {keys[key]: count for key, count in counts.items()}
