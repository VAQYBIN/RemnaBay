"""Рассылки (блок 11, экран А8). Только владелец: помощник рассылок не видит (01-domain)."""

from datetime import UTC, datetime

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from remnabay.broadcasts import (
    MAX_IMAGE_BYTES,
    BroadcastBusyError,
    BroadcastContent,
    BroadcastNotFoundError,
    BroadcastStateError,
    BroadcastView,
    FormatError,
    ImageError,
    NoRecipientsError,
    Segment,
    SegmentKind,
    check_ready,
    confirm,
    count_recipients,
    create_draft,
    delete_draft,
    get_broadcast,
    get_image,
    list_broadcasts,
    remove_image,
    send_test,
    set_image,
    stop,
    update_draft,
    view_broadcast,
)
from remnabay.domain.broadcasts import BroadcastState
from remnabay.messaging import (
    DeliveryError,
    NotAcceptedError,
    RecipientBlockedError,
    TelegramSender,
    UndeliverableError,
)
from remnabay.web._body import read_limited
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(prefix="/broadcasts", tags=["broadcasts"])

_NOT_FOUND = "Рассылка не найдена"


class TotalsOut(BaseModel):
    recipients: int
    delivered: int
    bot_blocked: int
    failed: int
    skipped: int
    pending: int


class BroadcastOut(BaseModel):
    id: int
    state: BroadcastState
    content: BroadcastContent
    segment: Segment
    has_image: bool
    created_at: datetime
    confirmed_at: datetime | None
    finished_at: datetime | None
    totals: TotalsOut


class BroadcastsOut(BaseModel):
    broadcasts: list[BroadcastOut]


class SegmentOut(BaseModel):
    kind: SegmentKind
    recipients: int


class SegmentsOut(BaseModel):
    segments: list[SegmentOut]


class DraftIn(BaseModel):
    content: BroadcastContent
    segment: Segment


class ConfirmedOut(BaseModel):
    recipients: int
    broadcast: BroadcastOut


def _out(view: BroadcastView) -> BroadcastOut:
    totals = view.totals
    return BroadcastOut(
        id=view.id,
        state=view.state,
        content=view.content,
        segment=view.segment,
        has_image=view.has_image,
        created_at=view.created_at,
        confirmed_at=view.confirmed_at,
        finished_at=view.finished_at,
        totals=TotalsOut(
            recipients=totals.recipients,
            delivered=totals.delivered,
            bot_blocked=totals.bot_blocked,
            failed=totals.failed,
            skipped=totals.skipped,
            pending=totals.pending,
        ),
    )


async def _view(session: DbSession, broadcast_id: int) -> BroadcastOut:
    try:
        return _out(await view_broadcast(session, broadcast_id))
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(error))


@router.get("")
async def broadcasts(session: DbSession, owner: Owner) -> BroadcastsOut:
    """История рассылок с итогами (11.8)."""
    del owner
    return BroadcastsOut(broadcasts=[_out(view) for view in await list_broadcasts(session)])


@router.get("/segments")
async def segments(session: DbSession, owner: Owner) -> SegmentsOut:
    """Сегменты с числом получателей сейчас (11.2)."""
    del owner
    now = datetime.now(UTC)
    return SegmentsOut(
        segments=[
            SegmentOut(
                kind=kind, recipients=await count_recipients(session, Segment(kind=kind), now)
            )
            for kind in SegmentKind
        ]
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: DraftIn, session: DbSession, owner: Owner) -> BroadcastOut:
    broadcast_id = await create_draft(session, body.content, body.segment, member_id=owner.id)
    await session.commit()
    return await _view(session, broadcast_id)


@router.get("/{broadcast_id}", responses={status.HTTP_404_NOT_FOUND: {}})
async def broadcast(broadcast_id: int, session: DbSession, owner: Owner) -> BroadcastOut:
    """Рассылка: содержимое, а у идущей — прогресс (11.5)."""
    del owner
    return await _view(session, broadcast_id)


@router.put(
    "/{broadcast_id}", responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_409_CONFLICT: {}}
)
async def update(
    broadcast_id: int, body: DraftIn, session: DbSession, owner: Owner
) -> BroadcastOut:
    del owner
    try:
        await update_draft(session, broadcast_id, body.content, body.segment)
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    await session.commit()
    return await _view(session, broadcast_id)


@router.delete(
    "/{broadcast_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_409_CONFLICT: {}},
)
async def delete(broadcast_id: int, session: DbSession, owner: Owner) -> None:
    del owner
    try:
        await delete_draft(session, broadcast_id)
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    await session.commit()


@router.put(
    "/{broadcast_id}/image",
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_409_CONFLICT: {},
        status.HTTP_413_CONTENT_TOO_LARGE: {},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {},
    },
)
async def upload_image(
    broadcast_id: int, request: Request, session: DbSession, owner: Owner
) -> BroadcastOut:
    """Изображение — телом запроса: JPEG или PNG до 10 МБ (11.1)."""
    del owner
    data = await read_limited(request, MAX_IMAGE_BYTES)
    try:
        await set_image(session, broadcast_id, data)
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    except ImageError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return await _view(session, broadcast_id)


@router.delete(
    "/{broadcast_id}/image", responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_409_CONFLICT: {}}
)
async def delete_image(broadcast_id: int, session: DbSession, owner: Owner) -> BroadcastOut:
    del owner
    try:
        await remove_image(session, broadcast_id)
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    await session.commit()
    return await _view(session, broadcast_id)


@router.get(
    "/{broadcast_id}/image",
    response_class=Response,
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def image(broadcast_id: int, session: DbSession, owner: Owner) -> Response:
    """Изображение для предпросмотра в админке."""
    del owner
    found = await get_image(session, broadcast_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Изображения нет")
    return Response(
        found.data, media_type=found.content_type, headers={"Cache-Control": "private, no-cache"}
    )


def _test_failure(error: DeliveryError) -> str:
    match error:
        case RecipientBlockedError():
            return (
                "Telegram не доставил сообщение: бот у вас заблокирован или вы ещё не "
                "открывали его. Откройте бот и повторите"
            )
        case UndeliverableError():
            return f"Telegram не принял сообщение: {error}"
        case NotAcceptedError():
            return "Telegram сейчас не принимает сообщения — повторите через минуту"
        case _:
            return "Неизвестно, дошло ли сообщение: Telegram не ответил. Проверьте бот"


@router.post(
    "/{broadcast_id}/test",
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {},
        status.HTTP_502_BAD_GATEWAY: {},
    },
)
async def test(broadcast_id: int, request: Request, session: DbSession, owner: Owner) -> None:
    """Тестовое сообщение себе (11.3): то же, что получат клиенты."""
    try:
        found = await get_broadcast(session, broadcast_id)
        await check_ready(session, found)
        bot: Bot = request.app.state.bot
        await send_test(
            session, TelegramSender.for_bot(bot), found, owner.telegram_id, member_id=owner.id
        )
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except FormatError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except DeliveryError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _test_failure(error)) from error
    await session.commit()


@router.post(
    "/{broadcast_id}/confirm",
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_409_CONFLICT: {},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {},
    },
)
async def start(broadcast_id: int, session: DbSession, owner: Owner) -> ConfirmedOut:
    """Подтверждение (11.4): получатели фиксируются сейчас, рассылка начинается."""
    try:
        count = await confirm(session, broadcast_id, member_id=owner.id, now=datetime.now(UTC))
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    except BroadcastBusyError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Уже идёт другая рассылка: дождитесь её окончания или остановите её",
        ) from error
    except FormatError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except NoRecipientsError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "В сегменте нет получателей"
        ) from error
    await session.commit()
    return ConfirmedOut(recipients=count, broadcast=await _view(session, broadcast_id))


@router.post(
    "/{broadcast_id}/stop", responses={status.HTTP_404_NOT_FOUND: {}, status.HTTP_409_CONFLICT: {}}
)
async def halt(broadcast_id: int, session: DbSession, owner: Owner) -> BroadcastOut:
    """«Остановить» (11.7)."""
    try:
        await stop(session, broadcast_id, member_id=owner.id)
    except BroadcastNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from error
    except BroadcastStateError as error:
        raise _conflict(error) from error
    await session.commit()
    return await _view(session, broadcast_id)
