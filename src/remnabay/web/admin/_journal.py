"""Экран «Журнал» (А10; 4.35): все записи журнала с фильтрами, только чтение (4.26).

Записи, не привязанные к клиенту (отклонённые вебхуки, рассылки, «Применить в
Telegram», миграция), видны здесь так же, как и записи о клиентах. Смотреть журнал
может любой участник команды: в таблице ролей (01-domain) он не закрыт, а журнал по
клиенту помощник и так видит в карточке клиента. Изменяющих эндпоинтов нет (4.26).
"""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, select, tuple_

from remnabay.domain.clients import TelegramAccount
from remnabay.domain.payments import Payment
from remnabay.domain.promo import PromoCode
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember
from remnabay.journal import ActorType, JournalEntry, JsonValue, Outcome
from remnabay.shop_settings import shop_time_zone
from remnabay.web.admin._deps import DbSession, Member
from remnabay.web.admin._payments import ClientRef

router = APIRouter(tags=["journal"])

# Записей на странице; следующая страница — по `before` (новые сверху)
PAGE_SIZE = 50

# Записи без объекта: например, отклонённый вебхук панели
NO_SUBJECT = "none"


class JournalSubjectOut(BaseModel):
    """Над чем: вид и id объекта и то, что помогает его узнать и открыть."""

    type: str
    id: str
    # Клиент объекта — для ссылки на карточку: сам клиент, владелец подписки, плательщик
    client: ClientRef | None = None
    # Понятное имя: название подписки или тарифа, код промокода, имя участника команды
    name: str | None = None


class JournalRowOut(BaseModel):
    id: int
    occurred_at: datetime
    actor_type: ActorType
    # Участник команды — по имени (10.7); провайдер — по коду
    actor: str | None
    # Клиент, если действовал клиент, — для ссылки на карточку
    actor_client: ClientRef | None
    action: str
    outcome: Outcome
    subject: JournalSubjectOut | None
    # Подробности как записаны: например, путь «вебхук или сверка» (4.9), причина отказа
    details: dict[str, JsonValue]


class JournalPageOut(BaseModel):
    entries: list[JournalRowOut]
    # Значение `before` для следующей страницы; `null` — записей дальше нет
    next_before: int | None
    # Даты в админке — в часовом поясе магазина (1.18)
    time_zone: str


# --- Имена для записей страницы: одним запросом на вид объекта ---


def _numeric_ids(values: Iterable[str | None]) -> set[int]:
    return {int(value) for value in values if value is not None and value.isdigit()}


async def _client_refs(session: DbSession, client_ids: set[int]) -> dict[int, ClientRef]:
    """Клиенты по первому Telegram-аккаунту, как в карточке клиента."""
    refs = {
        client_id: ClientRef(id=client_id, telegram_id=None, username=None, name=None)
        for client_id in client_ids
    }
    if not client_ids:
        return refs
    accounts = await session.scalars(
        select(TelegramAccount)
        .where(TelegramAccount.client_id.in_(client_ids))
        .order_by(TelegramAccount.created_at.desc())
    )
    # От новых к старым: в словаре остаётся первый аккаунт клиента
    for account in accounts:
        name = " ".join(part for part in (account.first_name, account.last_name) if part) or None
        refs[account.client_id] = ClientRef(
            id=account.client_id,
            telegram_id=account.telegram_id,
            username=account.username,
            name=name,
        )
    return refs


def _ids_of(entries: list[JournalEntry], subject_type: str) -> set[int]:
    return _numeric_ids(e.subject_id for e in entries if e.subject_type == subject_type)


async def _rows(session: DbSession, entries: list[JournalEntry]) -> list[JournalRowOut]:
    members = {
        member.id: member.name or f"ID {member.telegram_id}"
        for member in await session.scalars(select(TeamMember))
    }
    subscription_ids = _ids_of(entries, "subscription")
    subscriptions = {
        row.id: (row.name, row.client_id)
        for row in await session.execute(
            select(Subscription.id, Subscription.name, Subscription.client_id).where(
                Subscription.id.in_(subscription_ids)
            )
        )
    }
    payment_ids = _ids_of(entries, "payment")
    payment_clients = {
        row.id: row.client_id
        for row in await session.execute(
            select(Payment.id, Payment.client_id).where(Payment.id.in_(payment_ids))
        )
    }
    tariff_ids = _ids_of(entries, "tariff")
    tariffs = {
        row.id: row.name
        for row in await session.execute(
            select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids))
        )
    }
    promo_ids = _ids_of(entries, "promo_code")
    promo_codes = {
        row.id: row.code
        for row in await session.execute(
            select(PromoCode.id, PromoCode.code).where(PromoCode.id.in_(promo_ids))
        )
    }

    client_ids = _ids_of(entries, "client")
    client_ids |= _numeric_ids(e.actor_ref for e in entries if e.actor_type == ActorType.CLIENT)
    client_ids |= {client_id for _, client_id in subscriptions.values()}
    client_ids |= {client_id for client_id in payment_clients.values() if client_id is not None}
    clients = await _client_refs(session, client_ids)

    def subject(entry: JournalEntry) -> JournalSubjectOut | None:
        if entry.subject_type is None or entry.subject_id is None:
            return None
        out = JournalSubjectOut(type=entry.subject_type, id=entry.subject_id)
        if not entry.subject_id.isdigit():
            return out
        key = int(entry.subject_id)
        match entry.subject_type:
            case "client":
                out.client = clients.get(key)
            case "subscription":
                if key in subscriptions:
                    name, client_id = subscriptions[key]
                    out.name = name
                    out.client = clients.get(client_id)
            case "payment":
                client_id = payment_clients.get(key)
                out.client = clients.get(client_id) if client_id is not None else None
            case "tariff":
                out.name = tariffs.get(key)
            case "promo_code":
                out.name = promo_codes.get(key)
            case "team_member":
                out.name = members.get(key)
            case _:
                pass
        return out

    def actor(entry: JournalEntry) -> str | None:
        ref = entry.actor_ref
        if entry.actor_type == ActorType.TEAM_MEMBER and ref is not None and ref.isdigit():
            return members.get(int(ref), f"участник {ref}")
        return ref

    return [
        JournalRowOut(
            id=entry.id,
            occurred_at=entry.occurred_at,
            actor_type=entry.actor_type,
            actor=actor(entry),
            actor_client=clients.get(int(entry.actor_ref))
            if entry.actor_type == ActorType.CLIENT
            and entry.actor_ref is not None
            and entry.actor_ref.isdigit()
            else None,
            action=entry.action,
            outcome=entry.outcome,
            subject=subject(entry),
            details=entry.details,
        )
        for entry in entries
    ]


# --- Список ---

# Действие и группа действий — в том виде, в каком их пишет код: `payment.paid`, `payment`
_ACTION = r"^[a-z0-9_]+\.[a-z0-9_.]+$"
_ACTION_GROUP = r"^[a-z0-9_]+$"
_SUBJECT_TYPE = r"^[a-z0-9_]+$"


@router.get("/journal", responses={status.HTTP_404_NOT_FOUND: {}})
async def journal_list(
    session: DbSession,
    member: Member,
    actor_type: Annotated[ActorType | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=100, pattern=_ACTION)] = None,
    action_group: Annotated[str | None, Query(max_length=100, pattern=_ACTION_GROUP)] = None,
    subject_type: Annotated[str | None, Query(max_length=50, pattern=_SUBJECT_TYPE)] = None,
    subject_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    outcome: Annotated[Outcome | None, Query()] = None,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    before: Annotated[int | None, Query(gt=0)] = None,
) -> JournalPageOut:
    """А10 (4.35): записи журнала, новые сверху, по `PAGE_SIZE` на страницу.

    Фильтры: кто (`actor_type`), что (`action` — одно действие или `action_group` —
    все действия группы, например `payment`), над чем (`subject_type`, `none` — записи
    без объекта, и при желании `subject_id`), период (`since`, `until` — даты в часовом
    поясе магазина, 1.18), результат (`outcome`). `before` — id последней записи
    предыдущей страницы."""
    del member
    conditions: list[ColumnElement[bool]] = []
    if actor_type is not None:
        conditions.append(JournalEntry.actor_type == actor_type)
    if action is not None:
        conditions.append(JournalEntry.action == action)
    if action_group is not None:
        conditions.append(JournalEntry.action.startswith(f"{action_group}.", autoescape=True))
    if subject_type == NO_SUBJECT:
        conditions.append(JournalEntry.subject_type.is_(None))
    elif subject_type is not None:
        conditions.append(JournalEntry.subject_type == subject_type)
        if subject_id is not None:
            conditions.append(JournalEntry.subject_id == subject_id)
    if outcome is not None:
        conditions.append(JournalEntry.outcome == outcome)
    zone = await shop_time_zone(session)
    if since is not None:
        conditions.append(JournalEntry.occurred_at >= datetime.combine(since, time(), zone))
    if until is not None:
        end = datetime.combine(until, time(), zone) + timedelta(days=1)
        conditions.append(JournalEntry.occurred_at < end)
    if before is not None:
        anchor = await session.get(JournalEntry, before)
        if anchor is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Запись журнала не найдена")
        conditions.append(
            tuple_(JournalEntry.occurred_at, JournalEntry.id)
            < tuple_(anchor.occurred_at, anchor.id)
        )

    query: Select[JournalEntry] = (
        select(JournalEntry)
        .where(*conditions)
        .order_by(JournalEntry.occurred_at.desc(), JournalEntry.id.desc())
        .limit(PAGE_SIZE + 1)
    )
    entries = list(await session.scalars(query))
    more = len(entries) > PAGE_SIZE
    entries = entries[:PAGE_SIZE]
    return JournalPageOut(
        entries=await _rows(session, entries),
        next_before=entries[-1].id if more else None,
        time_zone=zone.key,
    )
