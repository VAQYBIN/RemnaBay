"""Ввод промокода клиентом: проверка кода и защита от перебора (8.4, 8.9–8.13, решение 0065).

- Если кода нет, промокод отключён, истёк или исчерпан, клиент видит один нейтральный
  текст `promo.error.invalid`: он не подсказывает, какие коды существуют (8.12).
  Остальные причины называются: код настоящий, клиенту нужно понять, что делать.
- Защита от перебора считает только попытки с нейтральным текстом; удачный ввод
  сбрасывает счётчик (8.13). Код, введённый не там (8.9, 0066), счётчик не меняет.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field, TypeAdapter
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Payment
from remnabay.domain.promo import PromoActivation, PromoCode, PromoEntry, PromoInput, PromoKind
from remnabay.domain.promo import promo_code_tariffs as code_tariffs
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffType
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.payments import disabled_in_panel, live_subscriptions
from remnabay.promo._codes import COUNTED, activations_count, normalize_code
from remnabay.shop_settings import ShopSetting, get_setting

CLIENT_SUBJECT = "client"
MAX_FAILED_ATTEMPTS = 100
MAX_PAUSE_MINUTES = 1440
# «Промокоды» → «Защита от перебора»: после 5 неверных кодов подряд — пауза 15 минут
# (8.13, 04-operator-settings)
PROMO_MAX_FAILED = ShopSetting(
    "promo.max_failed_attempts",
    TypeAdapter[int](Annotated[int, Field(ge=1, le=MAX_FAILED_ATTEMPTS)]),
    5,
)
PROMO_PAUSE_MINUTES = ShopSetting(
    "promo.pause_minutes",
    TypeAdapter[int](Annotated[int, Field(ge=1, le=MAX_PAUSE_MINUTES)]),
    15,
)
# Бот ждёт код следующим сообщением не дольше этого: позже сообщение — уже не код
AWAITING_LIFETIME = timedelta(hours=1)


class Rejection(StrEnum):
    """Почему промокод не принят; текст — `promo.error.<значение>` (03-bot-texts, З8)."""

    INVALID = "invalid"
    ALREADY_USED = "already_used"
    WRONG_TARIFF = "wrong_tariff"
    NEW_ONLY = "new_only"
    NO_SUBSCRIPTION = "no_subscription"
    RATE_LIMITED = "rate_limited"
    DAYS_ON_CONFIRM = "days_on_confirm"
    DISCOUNT_IN_MENU = "discount_in_menu"

    @property
    def text_key(self) -> str:
        return f"promo.error.{self.value}"


class PromoRejectedError(Exception):
    """Промокод не принят. `minutes` — через сколько минут можно снова (8.13)."""

    def __init__(self, reason: Rejection, minutes: int = 0) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.minutes = minutes


async def without_payments(session: AsyncSession, client: Client) -> bool:
    """Клиент без оплат (8.2, решение 0065): применённых платежей нет (платёж с нулевой
    суммой по скидке 100% считается) и клиент не перенесён с платной подпиской.

    Перенесённая подписка считается платной, если она не триальная: платежей старого
    бота магазин не видел (05-migration)."""
    paid = await session.scalar(
        select(exists().where(Payment.client_id == client.id, Payment.applied_at.is_not(None)))
    )
    if paid:
        return False
    if client.adopted_at is None:
        return True
    migrated_paid = await session.scalar(
        select(exists().where(Subscription.client_id == client.id, ~Subscription.is_trial))
    )
    return not migrated_paid


async def days_targets(session: AsyncSession, client_id: int) -> list[Subscription]:
    """Подписки, к которым можно добавить дни (8.10, решение 0065): со сроком, которые
    клиент может продлить, — не «только пакет» и не отключённые в панели; в том
    числе триальная."""
    targets: list[Subscription] = []
    for subscription in await live_subscriptions(session, client_id):
        if subscription.expires_at is None or disabled_in_panel(subscription):
            continue
        if subscription.tariff_id is not None:
            tariff = await session.get(Tariff, subscription.tariff_id)
            if tariff is not None and tariff.type == TariffType.PACKAGE_ONLY:
                continue
        targets.append(subscription)
    return targets


async def usable_promo(session: AsyncSession, code: str, now: datetime) -> PromoCode | None:
    """Промокод, который сейчас действует: есть, не отключён, не истёк, не исчерпан.
    Иначе — `None`: для клиента это один и тот же нейтральный ответ (8.12)."""
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == normalize_code(code)))
    if promo is None:
        return None
    return promo if await still_usable(session, promo, now) else None


async def still_usable(session: AsyncSession, promo: PromoCode, now: datetime) -> bool:
    if promo.disabled_at is not None or promo.kind not in (
        PromoKind.PERCENT_DISCOUNT,
        PromoKind.DAYS,
    ):
        return False
    if promo.valid_until is not None and promo.valid_until <= now:
        return False
    if promo.max_activations is None:
        return True
    return await activations_count(session, promo.id) < promo.max_activations


async def _check_client(session: AsyncSession, client: Client, promo: PromoCode) -> None:
    """Одна активация на клиента (8.2) и «только для клиентов без оплат»."""
    used = await session.scalar(
        select(
            exists().where(
                PromoActivation.promo_code_id == promo.id,
                PromoActivation.client_id == client.id,
                PromoActivation.state.in_(COUNTED),
            )
        )
    )
    if used:
        raise PromoRejectedError(Rejection.ALREADY_USED)
    if promo.only_without_payments and not await without_payments(session, client):
        raise PromoRejectedError(Rejection.NEW_ONLY)


async def check_discount(
    session: AsyncSession, client: Client, promo: PromoCode, tariff: Tariff
) -> None:
    """Скидка подходит клиенту и тарифу (8.2, 8.12); действует ли код — отдельно."""
    await _check_client(session, client, promo)
    tariff_ids = list(
        await session.scalars(
            select(code_tariffs.c.tariff_id).where(code_tariffs.c.promo_code_id == promo.id)
        )
    )
    if tariff_ids and tariff.id not in tariff_ids:
        raise PromoRejectedError(Rejection.WRONG_TARIFF)


async def check_days(session: AsyncSession, client: Client, promo: PromoCode) -> None:
    """Дни подходят клиенту (8.2, 8.11): есть подписка, к которой их добавить."""
    await _check_client(session, client, promo)
    if not await days_targets(session, client.id):
        raise PromoRejectedError(Rejection.NO_SUBSCRIPTION)


# --- Ожидание кода и защита от перебора ---


async def promo_entry(session: AsyncSession, client_id: int) -> PromoEntry:
    """Строка ввода клиента, заблокированная до конца транзакции."""
    entry = await session.get(PromoEntry, client_id, with_for_update=True)
    if entry is None:
        entry = PromoEntry(client_id=client_id, failed_attempts=0)
        session.add(entry)
        await session.flush()
    return entry


def _minutes_left(entry: PromoEntry, now: datetime) -> int:
    if entry.blocked_until is None or entry.blocked_until <= now:
        return 0
    return math.ceil((entry.blocked_until - now) / timedelta(minutes=1))


async def blocked_minutes(session: AsyncSession, client_id: int, now: datetime) -> int:
    """Через сколько минут клиент снова может вводить промокоды; 0 — уже может (8.13)."""
    entry = await session.get(PromoEntry, client_id)
    return _minutes_left(entry, now) if entry is not None else 0


async def await_code(
    session: AsyncSession,
    client_id: int,
    source: PromoInput,
    now: datetime,
    *,
    tariff_id: int | None = None,
    subscription_id: int | None = None,
) -> None:
    """«Ввести промокод»: следующее сообщение клиента — код (З8)."""
    entry = await promo_entry(session, client_id)
    entry.awaiting = source
    entry.tariff_id = tariff_id if source == PromoInput.CONFIRM else None
    entry.subscription_id = subscription_id if source == PromoInput.CONFIRM else None
    entry.requested_at = now


@dataclass(frozen=True)
class Awaited:
    """Откуда клиент вводит код: из меню или с экрана подтверждения тарифа."""

    source: PromoInput
    tariff_id: int | None
    subscription_id: int | None


async def awaiting(session: AsyncSession, client_id: int, now: datetime) -> bool:
    entry = await session.get(PromoEntry, client_id)
    return (
        entry is not None
        and entry.requested_at is not None
        and now - entry.requested_at <= AWAITING_LIFETIME
    )


async def take_awaited(session: AsyncSession, client_id: int, now: datetime) -> Awaited | None:
    """Бот ждал код этим сообщением — ожидание снимается: одно сообщение — одна попытка."""
    entry = await promo_entry(session, client_id)
    source, requested_at = entry.awaiting, entry.requested_at
    awaited = (
        Awaited(source, entry.tariff_id, entry.subscription_id)
        if source is not None
        and requested_at is not None
        and now - requested_at <= AWAITING_LIFETIME
        else None
    )
    entry.awaiting = None
    entry.tariff_id = None
    entry.subscription_id = None
    entry.requested_at = None
    return awaited


async def _guard(session: AsyncSession, client: Client, now: datetime) -> PromoEntry:
    entry = await promo_entry(session, client.id)
    minutes = _minutes_left(entry, now)
    if minutes:
        raise PromoRejectedError(Rejection.RATE_LIMITED, minutes)
    return entry


async def _invalid(
    session: AsyncSession, client: Client, entry: PromoEntry, now: datetime
) -> PromoRejectedError:
    """Неверный код (8.13): после нескольких подряд ввод ограничивается на время."""
    entry.failed_attempts += 1
    if entry.failed_attempts < await get_setting(session, PROMO_MAX_FAILED):
        return PromoRejectedError(Rejection.INVALID)
    pause = await get_setting(session, PROMO_PAUSE_MINUTES)
    entry.failed_attempts = 0
    entry.blocked_until = now + timedelta(minutes=pause)
    await record(
        session,
        actor=Actor.client(client.id),
        action="promo.input_limited",
        outcome=Outcome.FAILURE,
        subject=Subject(CLIENT_SUBJECT, client.id),
        details={"minutes": pause},
    )
    return PromoRejectedError(Rejection.INVALID)


def _succeeded(entry: PromoEntry, promo: PromoCode) -> None:
    """Удачный ввод сбрасывает счётчик (8.13) и запоминает принятый код."""
    entry.failed_attempts = 0
    entry.blocked_until = None
    entry.accepted_promo_id = promo.id


async def is_accepted(session: AsyncSession, client_id: int, promo_id: int) -> bool:
    """Клиент вводил этот код и магазин его принял. Номер промокода в данных кнопки
    клиент может подделать: без проверки так можно подобрать код, минуя ввод (8.13)."""
    entry = await session.get(PromoEntry, client_id)
    return entry is not None and entry.accepted_promo_id == promo_id


async def accept(session: AsyncSession, client_id: int, promo_id: int) -> None:
    """Код принят без ввода: тот же промокод при «Попробовать снова» (3.12)."""
    entry = await promo_entry(session, client_id)
    entry.accepted_promo_id = promo_id


async def enter_discount(
    session: AsyncSession, client: Client, text: str, tariff: Tariff, now: datetime
) -> PromoCode:
    """Код с экрана подтверждения (8.4, 8.9, 8.12, 8.13): скидка к тарифу."""
    entry = await _guard(session, client, now)
    promo = await usable_promo(session, text, now)
    if promo is None:
        raise await _invalid(session, client, entry, now)
    if promo.kind == PromoKind.DAYS:
        raise PromoRejectedError(Rejection.DAYS_ON_CONFIRM)
    await check_discount(session, client, promo, tariff)
    _succeeded(entry, promo)
    return promo


async def enter_days(session: AsyncSession, client: Client, text: str, now: datetime) -> PromoCode:
    """Код из главного меню (8.9–8.13): дни к подписке."""
    entry = await _guard(session, client, now)
    promo = await usable_promo(session, text, now)
    if promo is None:
        raise await _invalid(session, client, entry, now)
    if promo.kind != PromoKind.DAYS:
        raise PromoRejectedError(Rejection.DISCOUNT_IN_MENU)
    await check_days(session, client, promo)
    _succeeded(entry, promo)
    return promo
