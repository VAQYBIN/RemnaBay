"""Тарифы и их архивация (блок 2, экран А6). Только владелец (01-domain, таблица ролей)."""

from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, StringConstraints

from remnabay.domain.tariffs import Tariff, TariffState, TariffType
from remnabay.panel import PanelClient, PanelError, PanelUnavailableError
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import get_setting
from remnabay.tariffs import (
    LastOnSaleError,
    TariffError,
    TariffInUseError,
    TariffNotFoundError,
    TariffParams,
    Usage,
    archive_tariff,
    check_squads,
    create_tariff,
    delete_tariff,
    ensure_editable,
    get_tariff,
    list_tariffs,
    reorder_tariffs,
    restore_tariff,
    tariff_usage,
    update_tariff,
    used_tariffs,
)
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(tags=["tariffs"])

MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 1000
# Сто лет: срок дальше даты «бессрочно» (2099) не нужен
MAX_DURATION_DAYS = 36500
MAX_DEVICE_LIMIT = 1000
MAX_SQUADS = 100


class TariffOut(BaseModel):
    id: int
    name: str
    description: str
    type: TariffType
    state: TariffState
    duration_days: int | None
    price: Decimal
    device_limit: int
    squad_uuids: list[UUID]
    # Удалить нельзя, только архивировать: по тарифу были подписки, платежи… (2.8)
    in_use: bool


class TariffsOut(BaseModel):
    currency: str
    # Сначала в продаже — в порядке показа клиенту (2.4), затем архив
    tariffs: list[TariffOut]


type _Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)
]
type _Description = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_DESCRIPTION_LENGTH)
]


class TariffIn(BaseModel):
    """Тариф «срок + безлимит» (2.1); другие типы — в v1 (2.3)."""

    type: Literal[TariffType.TERM_UNLIMITED] = TariffType.TERM_UNLIMITED
    name: _Name
    description: _Description = ""
    duration_days: Annotated[int, Field(ge=1, le=MAX_DURATION_DAYS)]
    # Цена больше нуля: бесплатный доступ — это триал (0055)
    price: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
    # Лимит устройств обязателен, не меньше 1 (0054)
    device_limit: Annotated[int, Field(ge=1, le=MAX_DEVICE_LIMIT)]
    # Хотя бы один сквад, как у триала (0055)
    squad_uuids: Annotated[list[UUID], Field(min_length=1, max_length=MAX_SQUADS)]

    def params(self) -> TariffParams:
        return TariffParams(
            name=self.name,
            description=self.description,
            duration_days=self.duration_days,
            price=self.price,
            device_limit=self.device_limit,
            squad_uuids=tuple(self.squad_uuids),
        )


class ConfirmIn(BaseModel):
    # Владелец видел предупреждение «последний тариф в продаже» (2.9)
    confirm_last: bool = False


class OrderIn(BaseModel):
    tariff_ids: list[int]


class SquadOut(BaseModel):
    uuid: UUID
    name: str


class LastOnSaleOut(BaseModel):
    reason: Literal["last_on_sale"] = "last_on_sale"
    message: str


class InUseOut(BaseModel):
    reason: Literal["in_use"] = "in_use"
    message: str
    usage: list[Usage]


def _out(tariff: Tariff, in_use: bool) -> TariffOut:
    return TariffOut(
        id=tariff.id,
        name=tariff.name,
        description=tariff.description,
        type=tariff.type,
        state=tariff.state,
        duration_days=tariff.duration_days,
        price=tariff.price,
        device_limit=tariff.device_limit,
        squad_uuids=list(tariff.squad_uuids),
        in_use=in_use,
    )


async def _one(session: DbSession, tariff: Tariff) -> TariffOut:
    return _out(tariff, bool(await tariff_usage(session, tariff.id)))


async def _tariffs(session: DbSession) -> TariffsOut:
    used = await used_tariffs(session)
    return TariffsOut(
        currency=await get_setting(session, SHOP_CURRENCY),
        tariffs=[_out(tariff, tariff.id in used) for tariff in await list_tariffs(session)],
    )


def _error(error: TariffError) -> HTTPException:
    match error:
        case TariffNotFoundError():
            return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
        case LastOnSaleError():
            return HTTPException(
                status.HTTP_409_CONFLICT, LastOnSaleOut(message=str(error)).model_dump()
            )
        case TariffInUseError():
            body = InUseOut(message=str(error), usage=sorted(error.usage))
            return HTTPException(status.HTTP_409_CONFLICT, body.model_dump())
        case _:
            return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


def _panel_error(error: PanelError) -> HTTPException:
    if isinstance(error, PanelUnavailableError):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Панель недоступна — сквады не получить"
        )
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Панель ответила ошибкой: {error}")


async def _check_squads(request: Request, body: TariffIn, already: list[UUID]) -> None:
    panel: PanelClient = request.app.state.panel
    try:
        await check_squads(panel, body.squad_uuids, already=already)
    except PanelError as error:
        raise _panel_error(error) from error
    except TariffError as error:
        raise _error(error) from error


@router.get("/tariffs")
async def tariffs(session: DbSession, owner: Owner) -> TariffsOut:
    del owner
    return await _tariffs(session)


@router.get(
    "/panel/squads",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {}, status.HTTP_502_BAD_GATEWAY: {}},
)
async def squads(request: Request, owner: Owner) -> list[SquadOut]:
    """Сквады из панели — для выбора в форме тарифа (2.2)."""
    del owner
    panel: PanelClient = request.app.state.panel
    try:
        result = await panel.list_internal_squads()
    except PanelError as error:
        raise _panel_error(error) from error
    return [SquadOut(uuid=squad.uuid, name=squad.name) for squad in result.internal_squads]


@router.post("/tariffs", status_code=status.HTTP_201_CREATED)
async def create(body: TariffIn, request: Request, session: DbSession, owner: Owner) -> TariffOut:
    """Новый тариф «срок + безлимит» в продаже (2.1–2.3)."""
    await _check_squads(request, body, already=[])
    tariff = await create_tariff(session, body.params(), member_id=owner.id)
    await session.commit()
    return _out(tariff, in_use=False)


@router.put("/tariffs/order")
async def order(body: OrderIn, session: DbSession, owner: Owner) -> TariffsOut:
    """Порядок тарифов в продаже, который видит клиент (2.4)."""
    try:
        await reorder_tariffs(session, body.tariff_ids, member_id=owner.id)
    except TariffError as error:
        raise _error(error) from error
    await session.commit()
    return await _tariffs(session)


@router.put("/tariffs/{tariff_id}")
async def update(
    tariff_id: int, body: TariffIn, request: Request, session: DbSession, owner: Owner
) -> TariffOut:
    """Новые параметры применяются при следующей покупке или продлении (2.5, 2.6)."""
    try:
        current = await get_tariff(session, tariff_id)
        # Тип — до обращения к панели: ответ не зависит от того, доступна ли она
        ensure_editable(current)
        await _check_squads(request, body, already=list(current.squad_uuids))
        tariff = await update_tariff(session, tariff_id, body.params(), member_id=owner.id)
    except TariffError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, tariff)


_CONFLICTS: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {"model": LastOnSaleOut | InUseOut},
}


@router.post(
    "/tariffs/{tariff_id}/archive",
    responses={status.HTTP_409_CONFLICT: {"model": LastOnSaleOut}},
)
async def archive(tariff_id: int, body: ConfirmIn, session: DbSession, owner: Owner) -> TariffOut:
    """В архив (2.7); последний тариф в продаже — с подтверждением (2.9)."""
    try:
        tariff = await archive_tariff(
            session, tariff_id, member_id=owner.id, confirm_last=body.confirm_last
        )
    except TariffError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, tariff)


@router.post("/tariffs/{tariff_id}/restore")
async def restore(tariff_id: int, session: DbSession, owner: Owner) -> TariffOut:
    """Вернуть в продажу (2.7)."""
    try:
        tariff = await restore_tariff(session, tariff_id, member_id=owner.id)
    except TariffError as error:
        raise _error(error) from error
    await session.commit()
    return await _one(session, tariff)


@router.delete(
    "/tariffs/{tariff_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_CONFLICTS,
)
async def delete(
    tariff_id: int, session: DbSession, owner: Owner, confirm_last: bool = False
) -> None:
    """Удалить тариф без подписок и платежей; иначе — только архив (2.8).

    `confirm_last` — владелец видел предупреждение «последний тариф в продаже» (2.9).
    """
    try:
        await delete_tariff(session, tariff_id, member_id=owner.id, confirm_last=confirm_last)
    except TariffError as error:
        raise _error(error) from error
    await session.commit()
