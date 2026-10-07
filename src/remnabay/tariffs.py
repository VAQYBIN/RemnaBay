"""Тарифы в админке: создание, правка, порядок, архив, удаление (блок 2).

Тарифы читаются из базы при каждом запросе — изменения применяются без
перезапуска магазина (2.10). Правка тарифа не трогает действующие подписки
и уже созданные счета (2.5, 2.6): новые параметры применяются при следующей
покупке или продлении, а условия платежа фиксируются при его создании (3.7).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain._types import Money
from remnabay.domain.payments import Payment
from remnabay.domain.promo import promo_code_tariffs
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffState, TariffType
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record
from remnabay.panel import PanelClient

JOURNAL_SUBJECT = "tariff"


class TariffError(Exception):
    """Действие с тарифом невозможно; текст — для владельца."""


class TariffNotFoundError(TariffError):
    def __init__(self, tariff_id: int) -> None:
        super().__init__(f"Тариф {tariff_id} не найден")


class LastOnSaleError(TariffError):
    """Последний тариф в продаже уходит из продажи — нужно подтверждение (2.9)."""

    def __init__(self) -> None:
        super().__init__(
            "Это последний тариф в продаже: новые клиенты не смогут ничего купить. "
            "Подтвердите действие."
        )


class Usage(StrEnum):
    """Почему тариф нельзя удалить, только архивировать (2.8)."""

    SUBSCRIPTIONS = "subscriptions"
    PAYMENTS = "payments"
    PROMO_CODES = "promo_codes"
    SUCCESSOR = "successor"


class TariffInUseError(TariffError):
    def __init__(self, usage: set[Usage]) -> None:
        super().__init__("Тариф уже использовался — его можно только архивировать")
        self.usage = usage


class UnknownSquadsError(TariffError):
    def __init__(self, squads: Iterable[UUID]) -> None:
        listed = ", ".join(sorted(str(squad) for squad in squads))
        super().__init__(f"Таких сквадов нет в панели: {listed}")
        self.squads = set(squads)


@dataclass(frozen=True)
class TariffParams:
    """Параметры тарифа «срок + безлимит» (2.1)."""

    name: str
    description: str
    duration_days: int
    price: Money
    device_limit: int
    squad_uuids: tuple[UUID, ...]


def _actor(member_id: int) -> Actor:
    return Actor.team_member(member_id)


def _params_json(params: TariffParams) -> dict[str, JsonValue]:
    return {
        "name": params.name,
        "description": params.description,
        "duration_days": params.duration_days,
        "price": str(params.price),
        "device_limit": params.device_limit,
        "squad_uuids": [str(squad) for squad in params.squad_uuids],
    }


def _tariff_json(tariff: Tariff) -> dict[str, JsonValue]:
    return {
        "name": tariff.name,
        "description": tariff.description,
        "duration_days": tariff.duration_days,
        "price": str(tariff.price),
        "device_limit": tariff.device_limit,
        "squad_uuids": [str(squad) for squad in tariff.squad_uuids],
    }


async def _journal(
    session: AsyncSession,
    tariff_id: int,
    action: str,
    member_id: int,
    details: dict[str, JsonValue],
) -> None:
    await record(
        session,
        actor=_actor(member_id),
        action=action,
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, tariff_id),
        details=details,
    )


async def list_tariffs(session: AsyncSession) -> list[Tariff]:
    """Все тарифы: сначала в продаже — в порядке показа клиенту (2.4), затем остальные."""
    result = await session.scalars(
        select(Tariff).order_by(Tariff.state != TariffState.ON_SALE, Tariff.sort_order, Tariff.id)
    )
    return list(result)


async def tariffs_on_sale(session: AsyncSession) -> list[Tariff]:
    """Тарифы в продаже в порядке, заданном владельцем (2.4, 3.1)."""
    result = await session.scalars(
        select(Tariff)
        .where(Tariff.state == TariffState.ON_SALE)
        .order_by(Tariff.sort_order, Tariff.id)
    )
    return list(result)


async def get_tariff(session: AsyncSession, tariff_id: int) -> Tariff:
    tariff = await session.get(Tariff, tariff_id)
    if tariff is None:
        raise TariffNotFoundError(tariff_id)
    return tariff


async def _next_sort_order(session: AsyncSession) -> int:
    """Новый тариф и тариф, вернувшийся в продажу, встают в конец списка."""
    last = await session.scalar(
        select(func.max(Tariff.sort_order)).where(Tariff.state == TariffState.ON_SALE)
    )
    return 0 if last is None else last + 1


async def check_squads(
    panel: PanelClient, squads: Iterable[UUID], *, already: Iterable[UUID] = ()
) -> None:
    """Сквады выбираются из списка панели (2.2): новые сквады тарифа должны быть в панели.

    Уже сохранённые сквады не проверяются: правка описания не зависит от того,
    доступна ли панель. Ошибки панели (`PanelError`) — у вызывающего.
    """
    added = set(squads) - set(already)
    if not added:
        return
    known = {squad.uuid for squad in (await panel.list_internal_squads()).internal_squads}
    unknown = added - known
    if unknown:
        raise UnknownSquadsError(unknown)


async def create_tariff(session: AsyncSession, params: TariffParams, *, member_id: int) -> Tariff:
    """Новый тариф «срок + безлимит» в продаже (2.1, 2.3); встаёт в конец списка."""
    tariff = Tariff(
        name=params.name,
        description=params.description,
        type=TariffType.TERM_UNLIMITED,
        state=TariffState.ON_SALE,
        duration_days=params.duration_days,
        price=params.price,
        device_limit=params.device_limit,
        squad_uuids=list(params.squad_uuids),
        sort_order=await _next_sort_order(session),
    )
    session.add(tariff)
    await session.flush()
    await _journal(session, tariff.id, "tariff.created", member_id, _params_json(params))
    return tariff


async def update_tariff(
    session: AsyncSession, tariff_id: int, params: TariffParams, *, member_id: int
) -> Tariff:
    """Новые параметры тарифа. Действующие подписки и созданные счета не меняются (2.5, 2.6)."""
    tariff = await get_tariff(session, tariff_id)
    if tariff.type != TariffType.TERM_UNLIMITED:
        raise TariffError("В этой версии редактируются только тарифы «срок + безлимит»")
    old = _tariff_json(tariff)
    new = _params_json(params)
    changed: dict[str, JsonValue] = {
        key: [old[key], new[key]] for key in new if old[key] != new[key]
    }
    if not changed:
        return tariff
    tariff.name = params.name
    tariff.description = params.description
    tariff.duration_days = params.duration_days
    tariff.price = params.price
    tariff.device_limit = params.device_limit
    tariff.squad_uuids = list(params.squad_uuids)
    await session.flush()
    await _journal(session, tariff.id, "tariff.changed", member_id, changed)
    return tariff


async def is_last_on_sale(session: AsyncSession, tariff: Tariff) -> bool:
    """2.9: тариф — последний в продаже; уйти из продажи он может только с подтверждением.

    Тарифы в продаже блокируются до конца транзакции: два параллельных архивирования
    не оставят магазин без тарифов незаметно.
    """
    if tariff.state != TariffState.ON_SALE:
        return False
    on_sale = await session.scalars(
        select(Tariff.id).where(Tariff.state == TariffState.ON_SALE).with_for_update()
    )
    return list(on_sale) == [tariff.id]


async def archive_tariff(
    session: AsyncSession, tariff_id: int, *, member_id: int, confirm_last: bool = False
) -> Tariff:
    """Перевести тариф в архив (2.7): новым клиентам он не показывается."""
    tariff = await get_tariff(session, tariff_id)
    if tariff.state == TariffState.ARCHIVED:
        return tariff
    if tariff.state != TariffState.ON_SALE:
        raise TariffError("Архивировать можно только тариф в продаже")
    if not confirm_last and await is_last_on_sale(session, tariff):
        raise LastOnSaleError
    tariff.state = TariffState.ARCHIVED
    await session.flush()
    await _journal(session, tariff.id, "tariff.archived", member_id, {"name": tariff.name})
    return tariff


async def restore_tariff(session: AsyncSession, tariff_id: int, *, member_id: int) -> Tariff:
    """Вернуть тариф из архива в продажу (2.7); он встаёт в конец списка."""
    tariff = await get_tariff(session, tariff_id)
    if tariff.state == TariffState.ON_SALE:
        return tariff
    if tariff.state != TariffState.ARCHIVED:
        raise TariffError("Вернуть в продажу можно только тариф из архива")
    tariff.sort_order = await _next_sort_order(session)
    tariff.state = TariffState.ON_SALE
    await session.flush()
    await _journal(session, tariff.id, "tariff.restored", member_id, {"name": tariff.name})
    return tariff


async def tariff_usage(session: AsyncSession, tariff_id: int) -> set[Usage]:
    """Чем тариф уже занят. Пусто — тариф можно удалить (2.8)."""
    checks = {
        Usage.SUBSCRIPTIONS: exists().where(Subscription.tariff_id == tariff_id),
        Usage.PAYMENTS: exists().where(Payment.tariff_id == tariff_id),
        Usage.PROMO_CODES: exists().where(promo_code_tariffs.c.tariff_id == tariff_id),
        Usage.SUCCESSOR: exists().where(Tariff.successor_id == tariff_id),
    }
    return {usage for usage, check in checks.items() if await session.scalar(select(check))}


async def used_tariffs(session: AsyncSession) -> set[int]:
    """Тарифы, которые нельзя удалить (2.8), — для списка в админке одним проходом."""
    queries = [
        select(Subscription.tariff_id),
        select(Payment.tariff_id),
        select(promo_code_tariffs.c.tariff_id),
        select(Tariff.successor_id),
    ]
    used: set[int] = set()
    for query in queries:
        used.update(tariff_id for tariff_id in await session.scalars(query.distinct()) if tariff_id)
    return used


async def delete_tariff(
    session: AsyncSession, tariff_id: int, *, member_id: int, confirm_last: bool = False
) -> None:
    """Удалить тариф, который ещё не использовался (2.8); иначе — только архив.

    Подписка, созданная параллельно, не даст удалить тариф: база держит внешний ключ.
    """
    tariff = await get_tariff(session, tariff_id)
    usage = await tariff_usage(session, tariff_id)
    if usage:
        raise TariffInUseError(usage)
    if not confirm_last and await is_last_on_sale(session, tariff):
        raise LastOnSaleError
    details = {"type": tariff.type.value, **_tariff_json(tariff)}
    await session.delete(tariff)
    await session.flush()
    await _journal(session, tariff_id, "tariff.deleted", member_id, details)


async def reorder_tariffs(
    session: AsyncSession, tariff_ids: Sequence[int], *, member_id: int
) -> list[Tariff]:
    """Порядок тарифов в продаже, который видит клиент (2.4).

    Список — ровно все тарифы в продаже: иначе порядок собран по устаревшему
    экрану (тариф добавили или архивировали в другой вкладке).
    """
    on_sale = await session.scalars(
        select(Tariff).where(Tariff.state == TariffState.ON_SALE).with_for_update()
    )
    by_id = {tariff.id: tariff for tariff in on_sale}
    if len(tariff_ids) != len(set(tariff_ids)) or set(tariff_ids) != set(by_id):
        raise TariffError("Список тарифов изменился — обновите страницу и повторите")
    old = [tariff.id for tariff in sorted(by_id.values(), key=lambda t: (t.sort_order, t.id))]
    if old == list(tariff_ids):
        return [by_id[tariff_id] for tariff_id in tariff_ids]
    for position, tariff_id in enumerate(tariff_ids):
        by_id[tariff_id].sort_order = position
    await session.flush()
    await record(
        session,
        actor=_actor(member_id),
        action="tariff.reordered",
        outcome=Outcome.SUCCESS,
        details={"old": [*old], "new": [*tariff_ids]},
    )
    return [by_id[tariff_id] for tariff_id in tariff_ids]
