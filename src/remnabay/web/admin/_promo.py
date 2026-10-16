"""Промокоды (блок 8, экран А7) и «Настройки → Промокоды» (8.13).

Создаёт, меняет, отключает и включает промокоды только владелец; помощник видит
список и активации, чтобы отвечать клиентам (8.3, решение 0065).
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from remnabay.domain.promo import ActivationState, PromoActivation, PromoKind
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.promo import (
    MAX_ACTIVATIONS,
    MAX_CODE_LENGTH,
    MAX_DAYS,
    MAX_FAILED_ATTEMPTS,
    MAX_PAUSE_MINUTES,
    PROMO_MAX_FAILED,
    PROMO_PAUSE_MINUTES,
    CodeTakenError,
    NewPromo,
    PromoCodeError,
    PromoLimits,
    PromoNotFoundError,
    PromoState,
    PromoView,
    create_promo,
    generate_code,
    get_promo,
    list_promos,
    promo_activations,
    promo_value,
    set_disabled,
    update_limits,
    view_promo,
)
from remnabay.shop_settings import get_setting, set_setting, shop_time_zone
from remnabay.web.admin._deps import DbSession, Member, Owner
from remnabay.web.admin._payments import ClientRef, client_ref

router = APIRouter(tags=["promo"])

MAX_TARIFFS = 100


class PromoOut(BaseModel):
    id: int
    code: str
    kind: PromoKind
    # Процент скидки или число дней
    value: int
    # Последний день действия в часовом поясе магазина, включительно
    valid_until: date | None
    max_activations: int | None
    only_without_payments: bool
    tariff_ids: list[int]
    disabled: bool
    state: PromoState
    activations: int
    created_at: datetime


class TariffRefOut(BaseModel):
    id: int
    name: str
    state: TariffState


class PromoCodesOut(BaseModel):
    promo_codes: list[PromoOut]
    # Тарифы — для выбора ограничения и названий в списке
    tariffs: list[TariffRefOut]
    time_zone: str


class LimitsIn(BaseModel):
    """Ограничения (8.2); пусто — без ограничения. Тарифы — только у скидки. Срок —
    последний день действия в часовом поясе магазина, включительно."""

    valid_until: date | None = None
    max_activations: Annotated[int, Field(ge=1, le=MAX_ACTIVATIONS)] | None = None
    only_without_payments: bool = False
    tariff_ids: Annotated[list[int], Field(max_length=MAX_TARIFFS)] = []

    def limits(self, zone: ZoneInfo) -> PromoLimits:
        return PromoLimits(
            valid_until=_day_end(self.valid_until, zone),
            max_activations=self.max_activations,
            only_without_payments=self.only_without_payments,
            tariff_ids=tuple(dict.fromkeys(self.tariff_ids)),
        )


class PromoIn(LimitsIn):
    """Новый промокод (8.1): код, вид — скидка в процентах или дни, значение."""

    code: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CODE_LENGTH)
    ]
    kind: Literal[PromoKind.PERCENT_DISCOUNT, PromoKind.DAYS]
    value: Annotated[int, Field(ge=1, le=MAX_DAYS)]


class GeneratedOut(BaseModel):
    code: str


class ActivationOut(BaseModel):
    id: int
    client: ClientRef | None
    state: ActivationState
    payment_id: int | None
    subscription_id: int | None
    subscription_name: str | None
    created_at: datetime
    applied_at: datetime | None


class PromoSettingsOut(BaseModel):
    """«Защита от перебора»: после N неверных кодов подряд — пауза (8.13)."""

    max_failed_attempts: int
    pause_minutes: int


class PromoSettingsIn(BaseModel):
    max_failed_attempts: Annotated[int, Field(ge=1, le=MAX_FAILED_ATTEMPTS)]
    pause_minutes: Annotated[int, Field(ge=1, le=MAX_PAUSE_MINUTES)]


def _day_end(day: date | None, zone: ZoneInfo) -> datetime | None:
    """Конец последнего дня действия — начало следующего дня в часовом поясе магазина."""
    if day is None:
        return None
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)


def _last_day(moment: datetime | None, zone: ZoneInfo) -> date | None:
    if moment is None:
        return None
    return (moment.astimezone(zone) - timedelta(microseconds=1)).date()


def _out(view: PromoView, zone: ZoneInfo) -> PromoOut:
    promo = view.promo
    return PromoOut(
        id=promo.id,
        code=promo.code,
        kind=promo.kind,
        value=promo_value(promo),
        valid_until=_last_day(promo.valid_until, zone),
        max_activations=promo.max_activations,
        only_without_payments=promo.only_without_payments,
        tariff_ids=view.tariff_ids,
        disabled=promo.disabled_at is not None,
        state=view.state,
        activations=view.activations,
        created_at=promo.created_at,
    )


def _error(error: PromoCodeError) -> HTTPException:
    match error:
        case PromoNotFoundError():
            return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
        case CodeTakenError():
            return HTTPException(status.HTTP_409_CONFLICT, str(error))
        case _:
            return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


async def _one(session: DbSession, promo_id: int) -> PromoOut:
    promo = await get_promo(session, promo_id)
    await session.refresh(promo)
    view = await view_promo(session, promo, datetime.now(UTC))
    return _out(view, await shop_time_zone(session))


@router.get("/promo-codes")
async def promo_codes(session: DbSession, member: Member) -> PromoCodesOut:
    """А7: список — код, вид, значение, активации, срок, состояние (8.3)."""
    del member
    tariffs = await session.scalars(select(Tariff).order_by(Tariff.sort_order, Tariff.id))
    zone = await shop_time_zone(session)
    views = await list_promos(session, datetime.now(UTC))
    return PromoCodesOut(
        promo_codes=[_out(view, zone) for view in views],
        tariffs=[TariffRefOut(id=t.id, name=t.name, state=t.state) for t in tariffs],
        time_zone=(await shop_time_zone(session)).key,
    )


@router.get("/promo-codes/generate")
async def generate(session: DbSession, owner: Owner) -> GeneratedOut:
    """Свободный код из 8 знаков без похожих символов (8.1)."""
    del owner
    try:
        return GeneratedOut(code=await generate_code(session))
    except PromoCodeError as error:
        raise _error(error) from error


@router.post(
    "/promo-codes",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {}},
)
async def create(body: PromoIn, session: DbSession, owner: Owner) -> PromoOut:
    """8.1, 8.2: новый промокод."""
    limits = body.limits(await shop_time_zone(session))
    new = NewPromo(code=body.code, kind=body.kind, value=body.value, limits=limits)
    try:
        promo = await create_promo(session, new, member_id=owner.id)
    except PromoCodeError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, promo.id)


@router.put("/promo-codes/{promo_id}/limits", responses={status.HTTP_404_NOT_FOUND: {}})
async def change_limits(
    promo_id: int, body: LimitsIn, session: DbSession, owner: Owner
) -> PromoOut:
    """8.16: ограничения меняются в любой момент; код, вид и значение — нет."""
    try:
        limits = body.limits(await shop_time_zone(session))
        await update_limits(session, promo_id, limits, member_id=owner.id)
    except PromoCodeError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, promo_id)


async def _set_disabled(
    session: DbSession, promo_id: int, disabled: bool, member_id: int
) -> PromoOut:
    try:
        await set_disabled(session, promo_id, disabled, now=datetime.now(UTC), member_id=member_id)
    except PromoCodeError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, promo_id)


@router.post("/promo-codes/{promo_id}/disable", responses={status.HTTP_404_NOT_FOUND: {}})
async def disable(promo_id: int, session: DbSession, owner: Owner) -> PromoOut:
    """8.3: отключённый промокод ведёт себя как несуществующий."""
    return await _set_disabled(session, promo_id, True, owner.id)


@router.post("/promo-codes/{promo_id}/enable", responses={status.HTTP_404_NOT_FOUND: {}})
async def enable(promo_id: int, session: DbSession, owner: Owner) -> PromoOut:
    """8.3: отключённый промокод можно включить снова."""
    return await _set_disabled(session, promo_id, False, owner.id)


@router.get("/promo-codes/{promo_id}/activations", responses={status.HTTP_404_NOT_FOUND: {}})
async def activations(promo_id: int, session: DbSession, member: Member) -> list[ActivationOut]:
    """Активации промокода (8.3): кто, когда, к какому платежу или подписке."""
    del member
    try:
        found = await promo_activations(session, promo_id)
    except PromoCodeError as error:
        raise _error(error) from error
    result: list[ActivationOut] = []
    for activation in found:
        result.append(await _activation_out(session, activation))
    return result


async def _activation_out(session: DbSession, activation: PromoActivation) -> ActivationOut:
    subscription = (
        await session.get(Subscription, activation.subscription_id)
        if activation.subscription_id is not None
        else None
    )
    return ActivationOut(
        id=activation.id,
        client=await client_ref(session, activation.client_id),
        state=activation.state,
        payment_id=activation.payment_id,
        subscription_id=activation.subscription_id,
        subscription_name=subscription.name if subscription is not None else None,
        created_at=activation.created_at,
        applied_at=activation.applied_at,
    )


@router.get("/settings/promo")
async def promo_settings(session: DbSession, owner: Owner) -> PromoSettingsOut:
    del owner
    return PromoSettingsOut(
        max_failed_attempts=await get_setting(session, PROMO_MAX_FAILED),
        pause_minutes=await get_setting(session, PROMO_PAUSE_MINUTES),
    )


@router.put("/settings/promo")
async def update_promo_settings(
    body: PromoSettingsIn, session: DbSession, owner: Owner
) -> PromoSettingsOut:
    """«Настройки → Промокоды»: защита от перебора (8.13); изменение — в журнал."""
    for setting, value in (
        (PROMO_MAX_FAILED, body.max_failed_attempts),
        (PROMO_PAUSE_MINUTES, body.pause_minutes),
    ):
        if await get_setting(session, setting) != value:
            await set_setting(session, setting, value, member_id=owner.id)
    await session.commit()
    return await promo_settings(session, owner)
