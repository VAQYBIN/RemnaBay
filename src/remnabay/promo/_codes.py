"""Промокоды в админке: создание, ограничения, отключение, список и активации (8.1–8.3, 8.16).

- Код без учёта регистра: хранится и сравнивается в верхнем регистре, пробелы по
  краям отбрасываются; в коде — латинские буквы, цифры и дефис (решение 0065).
- Код, вид и значение после создания не меняются; ограничения — в любой момент
  (8.16). Удаления нет: на промокод ссылаются активации и платежи.
- Каждое действие команды — в журнал (4.25).
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ColumnElement, and_, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment, PaymentState
from remnabay.domain.promo import ActivationState, PromoActivation, PromoCode, PromoKind
from remnabay.domain.promo import promo_code_tariffs as code_tariffs
from remnabay.domain.tariffs import Tariff
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record

PROMO_SUBJECT = "promo_code"
MAX_CODE_LENGTH = 64
CODE_PATTERN = re.compile(r"[A-Z0-9-]+")
# Сгенерированный код — 8 знаков без похожих символов: нет 0 и O, 1 и I (решение 0065)
GENERATED_LENGTH = 8
GENERATED_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_GENERATE_ATTEMPTS = 20
# Дней по промокоду — как при выдаче дней командой: предел защищает от опечатки (0065)
MAX_DAYS = 3650
MAX_ACTIVATIONS = 1_000_000
# Виды MVP: скидка в процентах и дни; фиксированная скидка и бонусы — v1
MVP_KINDS = frozenset({PromoKind.PERCENT_DISCOUNT, PromoKind.DAYS})
# Активации, которые засчитаны: дни ждут панель или применены, скидка применена (8.6)
COUNTED = (ActivationState.PENDING, ActivationState.APPLIED)


class PromoCodeError(Exception):
    """Действие с промокодом невозможно; текст — для владельца."""


class PromoNotFoundError(PromoCodeError):
    def __init__(self, promo_id: int) -> None:
        super().__init__(f"Промокод {promo_id} не найден")


class CodeTakenError(PromoCodeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Промокод {code} уже есть — придумайте другой код")


def normalize_code(text: str) -> str:
    """Код в том виде, в каком он хранится: без пробелов по краям, в верхнем регистре."""
    return text.strip().upper()


def valid_code(code: str) -> bool:
    return len(code) <= MAX_CODE_LENGTH and CODE_PATTERN.fullmatch(code) is not None


@dataclass(frozen=True)
class PromoLimits:
    """Ограничения (8.2); пусто — без ограничения. Тарифы — только у скидки."""

    valid_until: datetime | None = None
    max_activations: int | None = None
    only_without_payments: bool = False
    tariff_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class NewPromo:
    code: str
    kind: PromoKind
    # Процент скидки или число дней
    value: int
    limits: PromoLimits


class PromoState(StrEnum):
    """Состояние промокода в списке (А7)."""

    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class PromoView:
    promo: PromoCode
    tariff_ids: list[int]
    activations: int
    state: PromoState


def promo_value(promo: PromoCode) -> int:
    """Процент скидки или число дней — смотря по виду."""
    value = promo.discount_percent if promo.kind == PromoKind.PERCENT_DISCOUNT else promo.days
    return value or 0


def promo_state(promo: PromoCode, activations: int, now: datetime) -> PromoState:
    if promo.disabled_at is not None:
        return PromoState.DISABLED
    if promo.valid_until is not None and promo.valid_until <= now:
        return PromoState.EXPIRED
    if promo.max_activations is not None and activations >= promo.max_activations:
        return PromoState.EXHAUSTED
    return PromoState.ACTIVE


def paid_waiting(promo_id: int) -> ColumnElement[bool]:
    """Платёж с промокодом оплачен, но ещё не применён: активации пока нет (8.6), но
    код уже использован. Особенно важно для платежа с нулевой суммой: он оплачен
    сразу, и, пока ждёт панель, клиент мог бы получить им ещё одну подписку (8.8)."""
    return and_(
        Payment.promo_code_id == promo_id,
        Payment.state.in_((PaymentState.PAID, PaymentState.PAID_NOT_APPLIED)),
        Payment.applied_at.is_(None),
    )


async def activations_count(session: AsyncSession, promo_id: int) -> int:
    """Засчитанные активации — для ограничения «общее число активаций» (8.2), вместе с
    оплаченными, но ещё не применёнными платежами с этим промокодом."""
    activations = await session.scalar(
        select(func.count())
        .select_from(PromoActivation)
        .where(PromoActivation.promo_code_id == promo_id, PromoActivation.state.in_(COUNTED))
    )
    waiting = await session.scalar(
        select(func.count()).select_from(Payment).where(paid_waiting(promo_id))
    )
    return (activations or 0) + (waiting or 0)


async def promo_tariffs(session: AsyncSession, promo_id: int) -> list[int]:
    result = await session.scalars(
        select(code_tariffs.c.tariff_id)
        .where(code_tariffs.c.promo_code_id == promo_id)
        .order_by(code_tariffs.c.tariff_id)
    )
    return list(result)


async def generate_code(session: AsyncSession) -> str:
    """Свободный код из 8 знаков без похожих символов (8.1)."""
    for _ in range(_GENERATE_ATTEMPTS):
        code = "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(GENERATED_LENGTH))
        if await _by_code(session, code) is None:
            return code
    raise PromoCodeError("Не удалось подобрать свободный код — попробуйте ещё раз")


async def _by_code(session: AsyncSession, code: str) -> PromoCode | None:
    return await session.scalar(select(PromoCode).where(PromoCode.code == code))


async def _check_limits(session: AsyncSession, kind: PromoKind, limits: PromoLimits) -> None:
    if limits.max_activations is not None and not 1 <= limits.max_activations <= MAX_ACTIVATIONS:
        raise PromoCodeError(f"Число активаций — от 1 до {MAX_ACTIVATIONS}")
    if not limits.tariff_ids:
        return
    if kind != PromoKind.PERCENT_DISCOUNT:
        raise PromoCodeError("Выбрать тарифы можно только у промокода на скидку")
    known = await session.scalars(select(Tariff.id).where(Tariff.id.in_(limits.tariff_ids)))
    if set(known) != set(limits.tariff_ids):
        raise PromoCodeError("Некоторых выбранных тарифов уже нет")


async def _save_tariffs(session: AsyncSession, promo_id: int, tariff_ids: Sequence[int]) -> None:
    await session.execute(delete(code_tariffs).where(code_tariffs.c.promo_code_id == promo_id))
    if tariff_ids:
        await session.execute(
            insert(code_tariffs),
            [{"promo_code_id": promo_id, "tariff_id": tariff_id} for tariff_id in tariff_ids],
        )


def _limits_journal(limits: PromoLimits) -> dict[str, JsonValue]:
    return {
        "valid_until": limits.valid_until.isoformat() if limits.valid_until else None,
        "max_activations": limits.max_activations,
        "only_without_payments": limits.only_without_payments,
        "tariff_ids": list(limits.tariff_ids),
    }


async def _journal(
    session: AsyncSession,
    promo: PromoCode,
    action: str,
    member_id: int,
    details: dict[str, JsonValue] | None = None,
) -> None:
    await record(
        session,
        actor=Actor.team_member(member_id),
        action=action,
        outcome=Outcome.SUCCESS,
        subject=Subject(PROMO_SUBJECT, promo.id),
        details={"code": promo.code, **(details or {})},
    )


async def create_promo(session: AsyncSession, new: NewPromo, *, member_id: int) -> PromoCode:
    """8.1, 8.2: код (свой или сгенерированный), вид, значение и ограничения."""
    code = normalize_code(new.code)
    if not valid_code(code):
        raise PromoCodeError(
            f"В коде — латинские буквы, цифры и дефис, не длиннее {MAX_CODE_LENGTH} знаков"
        )
    if new.kind not in MVP_KINDS:
        raise PromoCodeError("Этот вид промокода появится в следующих версиях")
    if new.kind == PromoKind.PERCENT_DISCOUNT and not 1 <= new.value <= 100:
        raise PromoCodeError("Скидка — от 1 до 100%")
    if new.kind == PromoKind.DAYS and not 1 <= new.value <= MAX_DAYS:
        raise PromoCodeError(f"Дней — от 1 до {MAX_DAYS}")
    await _check_limits(session, new.kind, new.limits)
    if await _by_code(session, code) is not None:
        raise CodeTakenError(code)
    promo = PromoCode(
        code=code,
        kind=new.kind,
        discount_percent=new.value if new.kind == PromoKind.PERCENT_DISCOUNT else None,
        days=new.value if new.kind == PromoKind.DAYS else None,
        valid_until=new.limits.valid_until,
        max_activations=new.limits.max_activations,
        only_without_payments=new.limits.only_without_payments,
        created_by_id=member_id,
    )
    session.add(promo)
    await session.flush()
    await _save_tariffs(session, promo.id, new.limits.tariff_ids)
    await _journal(
        session,
        promo,
        "promo.created",
        member_id,
        {"kind": new.kind.value, "value": new.value, **_limits_journal(new.limits)},
    )
    return promo


async def get_promo(session: AsyncSession, promo_id: int, *, lock: bool = False) -> PromoCode:
    promo = await session.get(PromoCode, promo_id, with_for_update=lock)
    if promo is None:
        raise PromoNotFoundError(promo_id)
    return promo


async def update_limits(
    session: AsyncSession, promo_id: int, limits: PromoLimits, *, member_id: int
) -> PromoCode:
    """8.16: ограничения меняются в любой момент; код, вид и значение — нет."""
    promo = await get_promo(session, promo_id, lock=True)
    await _check_limits(session, promo.kind, limits)
    promo.valid_until = limits.valid_until
    promo.max_activations = limits.max_activations
    promo.only_without_payments = limits.only_without_payments
    await _save_tariffs(session, promo.id, limits.tariff_ids)
    await _journal(session, promo, "promo.limits_changed", member_id, _limits_journal(limits))
    return promo


async def set_disabled(
    session: AsyncSession, promo_id: int, disabled: bool, *, now: datetime, member_id: int
) -> PromoCode:
    """8.3: отключённый промокод ведёт себя как несуществующий; включить можно снова."""
    promo = await get_promo(session, promo_id, lock=True)
    if (promo.disabled_at is not None) == disabled:
        return promo
    promo.disabled_at = now if disabled else None
    await _journal(session, promo, "promo.disabled" if disabled else "promo.enabled", member_id)
    return promo


async def view_promo(session: AsyncSession, promo: PromoCode, now: datetime) -> PromoView:
    activations = await activations_count(session, promo.id)
    return PromoView(
        promo=promo,
        tariff_ids=await promo_tariffs(session, promo.id),
        activations=activations,
        state=promo_state(promo, activations, now),
    )


async def list_promos(session: AsyncSession, now: datetime) -> list[PromoView]:
    """А7: все промокоды, новые сверху, с числом активаций и состоянием (8.3)."""
    promos = await session.scalars(
        select(PromoCode).order_by(PromoCode.created_at.desc(), PromoCode.id.desc())
    )
    tariffs: dict[int, list[int]] = {}
    for promo_id, tariff_id in await session.execute(
        select(code_tariffs.c.promo_code_id, code_tariffs.c.tariff_id).order_by(
            code_tariffs.c.tariff_id
        )
    ):
        tariffs.setdefault(promo_id, []).append(tariff_id)
    views: list[PromoView] = []
    for promo in promos:
        used = await activations_count(session, promo.id)
        views.append(
            PromoView(
                promo=promo,
                tariff_ids=tariffs.get(promo.id, []),
                activations=used,
                state=promo_state(promo, used, now),
            )
        )
    return views


async def promo_activations(session: AsyncSession, promo_id: int) -> list[PromoActivation]:
    """Активации промокода, новые сверху (8.3): помощник видит их, чтобы отвечать клиентам."""
    await get_promo(session, promo_id)
    result = await session.scalars(
        select(PromoActivation)
        .where(PromoActivation.promo_code_id == promo_id)
        .order_by(PromoActivation.created_at.desc(), PromoActivation.id.desc())
    )
    return list(result)
