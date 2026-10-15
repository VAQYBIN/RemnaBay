"""Рассылка в админке: черновик, изображение, подтверждение, остановка, история (11.1–11.8, 11.11).

Рассылки делает только владелец (01-domain, таблица ролей). Каждое изменение
состояния рассылки пишется в журнал (11.11).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.broadcasts._content import BroadcastContent, Segment
from remnabay.broadcasts._format import FormatError, check_length, parse
from remnabay.broadcasts._segments import recipients_query
from remnabay.broadcasts._sending import StepArgs, broadcast_subject, send_step
from remnabay.broadcasts._totals import Totals, totals_of
from remnabay.domain.broadcasts import (
    Broadcast,
    BroadcastImage,
    BroadcastRecipient,
    BroadcastState,
    DeliveryState,
)
from remnabay.journal import Actor, Outcome, record

# Ограничение Telegram на фото — 10 МБ
MAX_IMAGE_BYTES = 10 * 1024 * 1024
JPEG = "image/jpeg"
PNG = "image/png"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BroadcastNotFoundError(Exception):
    pass


class BroadcastStateError(Exception):
    """Действие не подходит состоянию рассылки; текст показывается владельцу."""


class BroadcastBusyError(Exception):
    """Уже идёт другая рассылка (11.13)."""


class NoRecipientsError(Exception):
    """В сегменте нет получателей — отправлять некому."""


class ImageError(Exception):
    """Файл не подходит; текст показывается владельцу."""


@dataclass(frozen=True)
class BroadcastView:
    id: int
    state: BroadcastState
    content: BroadcastContent
    segment: Segment
    has_image: bool
    created_at: datetime
    confirmed_at: datetime | None
    finished_at: datetime | None
    totals: Totals


async def _views(session: AsyncSession, broadcasts: list[Broadcast]) -> list[BroadcastView]:
    ids = [broadcast.id for broadcast in broadcasts]
    totals = await totals_of(session, ids)
    with_image = set(
        await session.scalars(
            select(BroadcastImage.broadcast_id).where(BroadcastImage.broadcast_id.in_(ids))
        )
    )
    return [
        BroadcastView(
            id=broadcast.id,
            state=broadcast.state,
            content=BroadcastContent.model_validate(broadcast.content),
            segment=Segment.model_validate(broadcast.segment),
            has_image=broadcast.id in with_image,
            created_at=broadcast.created_at,
            confirmed_at=broadcast.confirmed_at,
            finished_at=broadcast.finished_at,
            totals=totals[broadcast.id],
        )
        for broadcast in broadcasts
    ]


async def list_broadcasts(session: AsyncSession) -> list[BroadcastView]:
    """История рассылок с итогами, новые сверху (11.8); черновики — тоже."""
    broadcasts = await session.scalars(select(Broadcast).order_by(Broadcast.id.desc()))
    return await _views(session, list(broadcasts))


async def view_broadcast(session: AsyncSession, broadcast_id: int) -> BroadcastView:
    return (await _views(session, [await _get(session, broadcast_id)]))[0]


async def _get(session: AsyncSession, broadcast_id: int, *, lock: bool = False) -> Broadcast:
    query = select(Broadcast).where(Broadcast.id == broadcast_id)
    if lock:
        query = query.with_for_update()
    broadcast = await session.scalar(query)
    if broadcast is None:
        raise BroadcastNotFoundError(broadcast_id)
    return broadcast


async def _draft(session: AsyncSession, broadcast_id: int) -> Broadcast:
    broadcast = await _get(session, broadcast_id, lock=True)
    if broadcast.state != BroadcastState.DRAFT:
        raise BroadcastStateError("Рассылка уже отправлена — менять её нельзя")
    return broadcast


async def create_draft(
    session: AsyncSession, content: BroadcastContent, segment: Segment, *, member_id: int
) -> int:
    broadcast = Broadcast(
        content=content.model_dump(mode="json"),
        segment=segment.model_dump(mode="json"),
        created_by_id=member_id,
    )
    session.add(broadcast)
    await session.flush()
    return broadcast.id


async def update_draft(
    session: AsyncSession, broadcast_id: int, content: BroadcastContent, segment: Segment
) -> None:
    broadcast = await _draft(session, broadcast_id)
    broadcast.content = content.model_dump(mode="json")
    broadcast.segment = segment.model_dump(mode="json")


async def delete_draft(session: AsyncSession, broadcast_id: int) -> None:
    """Черновик можно удалить: его никто не получал. Отправленные остаются в истории."""
    await _draft(session, broadcast_id)
    await session.execute(delete(Broadcast).where(Broadcast.id == broadcast_id))


def image_type(data: bytes) -> str:
    """JPEG или PNG — их Telegram принимает как фото."""
    if data.startswith(_JPEG_SIGNATURE):
        return JPEG
    if data.startswith(_PNG_SIGNATURE):
        return PNG
    raise ImageError("Изображение — JPEG или PNG")


async def set_image(session: AsyncSession, broadcast_id: int, data: bytes) -> None:
    """Одно изображение (11.1): новое заменяет прежнее."""
    await _draft(session, broadcast_id)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("Изображение — не больше 10 МБ")
    content_type = image_type(data)
    await session.execute(
        insert(BroadcastImage)
        .values(broadcast_id=broadcast_id, content_type=content_type, data=data)
        .on_conflict_do_update(
            index_elements=[BroadcastImage.broadcast_id],
            set_={"content_type": content_type, "data": data, "telegram_file_id": None},
        )
    )


async def remove_image(session: AsyncSession, broadcast_id: int) -> None:
    await _draft(session, broadcast_id)
    await session.execute(delete(BroadcastImage).where(BroadcastImage.broadcast_id == broadcast_id))


async def get_image(session: AsyncSession, broadcast_id: int) -> BroadcastImage | None:
    return await session.get(BroadcastImage, broadcast_id)


async def check_ready(session: AsyncSession, broadcast: Broadcast) -> None:
    """Содержимое можно отправлять: разметка верна, длина в пределах, есть что отправить."""
    content = BroadcastContent.model_validate(broadcast.content)
    with_image = await session.get(BroadcastImage, broadcast.id) is not None
    text = parse(content.text)
    if not text.text and not with_image:
        raise FormatError("Напишите текст или добавьте изображение")
    check_length(text, with_image=with_image)


async def confirm(
    session: AsyncSession, broadcast_id: int, *, member_id: int, now: datetime
) -> int:
    """Подтверждение (11.4): состав получателей фиксируется сейчас, рассылка начинается.
    Возвращает число получателей."""
    broadcast = await _draft(session, broadcast_id)
    await check_ready(session, broadcast)
    sending = await session.scalar(
        select(Broadcast.id).where(Broadcast.state == BroadcastState.SENDING)
    )
    if sending is not None:
        raise BroadcastBusyError(sending)
    segment = Segment.model_validate(broadcast.segment)
    recipients = recipients_query(segment, now)
    await session.execute(
        insert(BroadcastRecipient).from_select(
            ["broadcast_id", "client_id"],
            select(literal(broadcast_id), recipients.subquery().c.id),
        )
    )
    count = await session.scalar(
        select(func.count()).where(BroadcastRecipient.broadcast_id == broadcast_id)
    )
    if not count:
        raise NoRecipientsError
    broadcast.state = BroadcastState.SENDING
    broadcast.confirmed_at = now
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as error:
        # Другую рассылку подтвердили одновременно с этой (11.13)
        raise BroadcastBusyError from error
    first = await session.scalar(
        select(func.min(BroadcastRecipient.client_id)).where(
            BroadcastRecipient.broadcast_id == broadcast_id
        )
    )
    await send_step.enqueue(session, StepArgs(broadcast_id=broadcast_id, client_id=first or 0))
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="broadcast.started",
        outcome=Outcome.SUCCESS,
        subject=broadcast_subject(broadcast_id),
        details={"segment": segment.kind.value, "recipients": count},
    )
    return count


async def stop(session: AsyncSession, broadcast_id: int, *, member_id: int) -> None:
    """Остановка (11.7): оставшимся получателям сообщение не отправляется. Сообщение,
    которое уходит прямо сейчас, остановка дожидается."""
    stopped = await session.scalar(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id, Broadcast.state == BroadcastState.SENDING)
        .values(state=BroadcastState.STOPPED, finished_at=func.now())
        .returning(Broadcast.id)
    )
    if stopped is None:
        await _get(session, broadcast_id)
        raise BroadcastStateError("Рассылка уже не идёт")
    await session.execute(
        update(BroadcastRecipient)
        .where(
            BroadcastRecipient.broadcast_id == broadcast_id,
            BroadcastRecipient.state == DeliveryState.PENDING,
        )
        .values(state=DeliveryState.SKIPPED)
    )
    totals = (await totals_of(session, [broadcast_id]))[broadcast_id]
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="broadcast.stopped",
        outcome=Outcome.SUCCESS,
        subject=broadcast_subject(broadcast_id),
        details=totals.as_details(),
    )


async def get_broadcast(session: AsyncSession, broadcast_id: int) -> Broadcast:
    return await _get(session, broadcast_id)
