"""Что клиент может купить или продлить (3.1–3.3, 3.16, 3.20, 3.21, 3.35, 3.36, 4.7).

Общие правила для бота и для проверки при создании счёта: кнопку можно нажать в
старом сообщении, а данные кнопки клиент может подделать.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import PaymentPurpose
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import Tariff, TariffState, TariffType


class CatalogError(Exception):
    """Эту покупку или продление сейчас оформить нельзя."""


async def tariffs_on_sale(session: AsyncSession) -> list[Tariff]:
    """Тарифы в продаже в порядке, заданном владельцем (2.4, 3.1). В MVP клиенту
    продаётся только «срок + безлимит» (2.3)."""
    result = await session.scalars(
        select(Tariff)
        .where(Tariff.state == TariffState.ON_SALE, Tariff.type == TariffType.TERM_UNLIMITED)
        .order_by(Tariff.sort_order, Tariff.id)
    )
    return list(result)


async def live_subscriptions(session: AsyncSession, client_id: int) -> list[Subscription]:
    """Подписки, которые клиент видит: удалённые в панели не показываются (4.6)."""
    result = await session.scalars(
        select(Subscription)
        .where(Subscription.client_id == client_id, Subscription.deleted_at.is_(None))
        .order_by(Subscription.created_at, Subscription.id)
    )
    return list(result)


def is_active(subscription: Subscription, now: datetime) -> bool:
    """Подписка ещё действует: срок по последним данным панели не истёк."""
    return subscription.expires_at is not None and subscription.expires_at > now


def disabled_in_panel(subscription: Subscription) -> bool:
    """Подписку отключили в панели: продлить и купить к ней нельзя (3.36, 4.7)."""
    return subscription.panel_status == PanelUserStatus.DISABLED


def trial_for_purchase(subscriptions: list[Subscription]) -> Subscription | None:
    """Триальная подписка, к которой применится покупка: если она у клиента
    единственная (3.16, 3.36)."""
    if len(subscriptions) == 1 and subscriptions[0].is_trial:
        return subscriptions[0]
    return None


def can_purchase(subscriptions: list[Subscription]) -> bool:
    """Купить подписку можно без подписок или с единственной триальной, если её не
    отключили в панели (3.36). Ещё одна подписка — v1 (лимит подписок)."""
    if not subscriptions:
        return True
    trial = trial_for_purchase(subscriptions)
    return trial is not None and not disabled_in_panel(trial)


async def renewal_tariff(
    session: AsyncSession, subscription: Subscription, now: datetime
) -> Tariff | None:
    """Тариф, которым подписка продлевается сразу, без выбора (3.20).

    `None` — нужно выбрать тариф из тех, что в продаже: у подписки нет тарифа
    (3.35) или её тариф в архиве, а подписка истекла (3.21).
    """
    if subscription.tariff_id is None:
        return None
    tariff = await session.get(Tariff, subscription.tariff_id)
    if tariff is None or tariff.type != TariffType.TERM_UNLIMITED:
        return None
    if tariff.state == TariffState.ON_SALE:
        return tariff
    if tariff.state == TariffState.ARCHIVED and is_active(subscription, now):
        return tariff
    return None


def can_renew(subscription: Subscription) -> bool:
    """«Продлить» есть у платной подписки, которую не отключили в панели (4.7, 3.36)."""
    return not subscription.is_trial and not disabled_in_panel(subscription)


async def check_choice(
    session: AsyncSession,
    *,
    client_id: int,
    purpose: PaymentPurpose,
    tariff: Tariff,
    subscription: Subscription | None,
    now: datetime,
) -> None:
    """Можно ли оформить именно это: покупку или продление выбранным тарифом.

    `CatalogError` — нельзя (тариф сняли с продажи, подписку отключили, кнопка
    из старого сообщения)."""
    subscriptions = await live_subscriptions(session, client_id)
    if purpose == PaymentPurpose.PURCHASE:
        if subscription is not None or not can_purchase(subscriptions):
            raise CatalogError("Покупка сейчас недоступна")
        if tariff not in await tariffs_on_sale(session):
            raise CatalogError("Тариф не в продаже")
        return
    if purpose != PaymentPurpose.RENEWAL or subscription is None:
        raise CatalogError("Неизвестная операция")
    if subscription not in subscriptions or not can_renew(subscription):
        raise CatalogError("Продление этой подписки недоступно")
    if tariff in await tariffs_on_sale(session):
        return
    if tariff == await renewal_tariff(session, subscription, now):
        return
    raise CatalogError("Этим тарифом подписку не продлить")


@dataclass(frozen=True)
class Quote:
    """Экран подтверждения (3.3): тариф, срок, цена, итог; новая дата окончания —
    если оплата добавит дни к существующей подписке."""

    tariff: Tariff
    days: int
    price: Decimal
    total: Decimal
    new_end: datetime | None


def quote(tariff: Tariff, subscription: Subscription | None, now: datetime) -> Quote:
    days = tariff.duration_days or 0
    new_end = None
    if subscription is not None:
        # Активная — от даты окончания, истёкшая — от момента оплаты (3.17, 3.18)
        base = subscription.expires_at
        if base is None or base <= now:
            base = now
        new_end = base + timedelta(days=days)
    return Quote(tariff=tariff, days=days, price=tariff.price, total=tariff.price, new_end=new_end)
