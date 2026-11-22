"""Запросы клиента на возврат (сценарий «Запрос возврата», 10.6, решение 0065).

Запрос фиксирует любой участник команды — запись с комментарием в карточке клиента.
Закрывается возвратом или отказом с комментарием; клиент получает уведомление о
результате. Отказать может только владелец: решение о возврате за ним. Каждое
действие — в журнале с участником и комментарием (10.7).
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import Refund, RefundRequest, RefundRequestState
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_BUTTON, send_to_client

CLIENT_SUBJECT = "client"


class RefundRequestError(Exception):
    """Действие с запросом недоступно; текст показывается участнику команды."""


def _comment(comment: str) -> str:
    comment = comment.strip()
    if not comment:
        raise RefundRequestError("Нужен комментарий: он попадает в журнал и карточку клиента")
    return comment


async def open_requests(session: AsyncSession, client_id: int) -> list[RefundRequest]:
    """Открытые запросы клиента, от старых к новым."""
    result = await session.scalars(
        select(RefundRequest)
        .where(
            RefundRequest.client_id == client_id,
            RefundRequest.state == RefundRequestState.OPEN,
        )
        .order_by(RefundRequest.id)
    )
    return list(result)


async def file_request(
    session: AsyncSession, client_id: int, *, comment: str, member_id: int
) -> RefundRequest:
    """Участник команды фиксирует запрос клиента на возврат (10.6)."""
    comment = _comment(comment)
    if await session.get(Client, client_id) is None:
        raise RefundRequestError("Клиент не найден")
    request = RefundRequest(client_id=client_id, comment=comment, created_by_id=member_id)
    session.add(request)
    await session.flush()
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="refund_request.filed",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, client_id),
        details={"request_id": request.id, "comment": comment},
    )
    return request


async def _open_request(session: AsyncSession, client_id: int, request_id: int) -> RefundRequest:
    request = await session.get(RefundRequest, request_id, with_for_update=True)
    if request is None or request.client_id != client_id:
        raise RefundRequestError("Запрос на возврат не найден")
    if request.state != RefundRequestState.OPEN:
        raise RefundRequestError("Запрос уже закрыт")
    return request


async def decline_request(
    session: AsyncSession,
    client_id: int,
    request_id: int,
    *,
    comment: str,
    member_id: int,
    now: datetime,
) -> RefundRequest:
    """Владелец отказывает в возврате с комментарием (10.6); клиент получает «В возврате
    отказано» (С14). Комментарий виден только команде. Права проверяет вызывающий."""
    comment = _comment(comment)
    request = await _open_request(session, client_id, request_id)
    request.state = RefundRequestState.REJECTED
    request.closed_at = now
    request.closed_by_id = member_id
    request.close_comment = comment
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="refund_request.declined",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, client_id),
        details={"request_id": request.id, "comment": comment},
    )
    await send_to_client(session, client_id, "event.refund_declined", buttons=[MAIN_MENU_BUTTON])
    return request


async def request_closed_by(
    session: AsyncSession, client_id: int | None, request_id: int | None
) -> RefundRequest | None:
    """Какой запрос закрывает возврат (10.6, решение 0065): выбранный владельцем; если
    выбора нет — единственный открытый. Из нескольких открытых выбрать обязательно."""
    if client_id is None:
        if request_id is not None:
            raise RefundRequestError("У неизвестного платежа нет запросов на возврат")
        return None
    if request_id is not None:
        return await _open_request(session, client_id, request_id)
    requests = await open_requests(session, client_id)
    if len(requests) > 1:
        raise RefundRequestError("У клиента несколько открытых запросов — выберите, какой закрыть")
    return requests[0] if requests else None


async def close_by_refund(
    session: AsyncSession, request: RefundRequest, refund: Refund, *, member_id: int
) -> None:
    """Возврат закрывает запрос (10.6). Клиент получает сообщение о самом возврате."""
    request.state = RefundRequestState.DONE
    request.refund_id = refund.id
    request.closed_at = refund.performed_at
    request.closed_by_id = member_id
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="refund_request.done",
        outcome=Outcome.SUCCESS,
        subject=Subject(CLIENT_SUBJECT, request.client_id),
        details={"request_id": request.id, "payment_id": refund.payment_id},
    )
