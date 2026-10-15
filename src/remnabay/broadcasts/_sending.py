"""Отправка рассылки: сообщение, шаги в очереди, тестовое сообщение владельцу (11.3, 11.5–11.9).

Рассылка идёт цепочкой небольших задач (4.28): шаг отправляет сообщение одному
получателю и ставит шаг для следующего. Поэтому:
- после перезапуска магазина цепочка продолжается с места остановки — шаг лежит в
  очереди (11.6);
- шаг сверяет состояние перед отправкой: остановленная рассылка дальше не идёт (11.7);
- сообщение, отправка которого оборвалась сбоем, повторно не отправляется: повтор —
  только если прошлая попытка точно ничего не отправила, как у служебных сообщений (4.33);
- пока ждут служебные сообщения, шаг уступает им очередь (11.13);
- шаги идут не чаще `SEND_INTERVAL`: общий предел Telegram — около 30 сообщений в
  секунду, остальное остаётся служебным сообщениям (11.5).
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.broadcasts._content import BroadcastContent
from remnabay.broadcasts._format import check_length, parse
from remnabay.broadcasts._totals import totals_of
from remnabay.domain.broadcasts import (
    Broadcast,
    BroadcastImage,
    BroadcastRecipient,
    BroadcastState,
    DeliveryState,
)
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import (
    MAIN_MENU_CALLBACK,
    AmbiguousDeliveryError,
    Button,
    NotAcceptedError,
    OutgoingMessage,
    Photo,
    RecipientBlockedError,
    Sender,
    UndeliverableError,
    load_texts,
    mark_blocked,
    send_message,
)
from remnabay.queue import AttemptResult, RejectedError, TaskContext, due_tasks_exist, task
from remnabay.texts import Texts

JOURNAL_SUBJECT = "broadcast"
# Кнопки «Купить» и «Продлить» рассылки — разбирает бот (11.10, 11.12)
BUY_CALLBACK = "bcast:buy"
RENEW_CALLBACK = "bcast:renew"
# Не чаще 20 сообщений рассылки в секунду (11.5)
SEND_INTERVAL = timedelta(milliseconds=50)
# Сколько рассылка ждёт, уступив очередь служебным сообщениям (11.13)
YIELD_DELAY = timedelta(seconds=1)
_SUCCESS = "success"
# Прошлая попытка точно не отправила сообщение: её не было или Telegram отказал
_SAFE_TO_SEND = (None, AttemptResult.REJECTED)
_ERROR_LIMIT = 500
_ABORTED = "Отправка оборвалась сбоем — сообщение могло уйти, повторно не отправляется"
_NOT_ACCEPTED = "Telegram так и не принял сообщение"

# Когда этот процесс последний раз отправил сообщение рассылки (time.monotonic)
_last_sent = 0.0


def broadcast_subject(broadcast_id: int) -> Subject:
    return Subject(JOURNAL_SUBJECT, broadcast_id)


def build_message(
    content: BroadcastContent, texts: Texts, language: str | None, photo: Photo | None
) -> OutgoingMessage:
    """Сообщение рассылки (С13): текст, изображение и кнопки; «Главное меню» — всегда.

    Целевое действие одно — «Купить» или «Продлить» — зелёное; если владелец добавил
    обе, ни одна не выделяется: на экране не больше одной зелёной кнопки (03-screens).
    """
    formatted = parse(content.text)
    check_length(formatted, with_image=photo is not None)
    style = _SUCCESS if content.buy != content.renew else None
    buttons: list[Button] = []
    if content.buy:
        buttons.append(
            Button(texts.render("btn.buy", language), callback_data=BUY_CALLBACK, style=style)
        )
    if content.renew:
        buttons.append(
            Button(texts.render("btn.renew", language), callback_data=RENEW_CALLBACK, style=style)
        )
    buttons.extend(Button(link.text, url=link.url) for link in content.links)
    buttons.append(
        Button(texts.render("btn.main_menu", language), callback_data=MAIN_MENU_CALLBACK)
    )
    return OutgoingMessage(formatted.text, tuple(buttons), entities=formatted.entities, photo=photo)


def _photo(image: BroadcastImage | None) -> Photo | None:
    if image is None:
        return None
    if image.telegram_file_id is not None:
        return Photo(file_id=image.telegram_file_id)
    return Photo(data=image.data, filename=_filename(image.content_type))


def _filename(content_type: str) -> str:
    return "image.png" if content_type == "image/png" else "image.jpg"


def _remember_file(image: BroadcastImage | None, file_id: str | None) -> None:
    """Загруженное в Telegram изображение следующие сообщения не загружают заново."""
    if image is not None and file_id is not None and image.telegram_file_id is None:
        image.telegram_file_id = file_id


async def send_test(
    session: AsyncSession, sender: Sender, broadcast: Broadcast, chat_id: int, *, member_id: int
) -> None:
    """Тестовое сообщение владельцу — то же, что получат клиенты (11.3). Ошибки
    отправки — подклассы `DeliveryError`; ошибки содержимого — `FormatError`."""
    content = BroadcastContent.model_validate(broadcast.content)
    image = await session.get(BroadcastImage, broadcast.id)
    message = build_message(content, await load_texts(session), None, _photo(image))
    _remember_file(image, await sender.send(chat_id, message))
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="broadcast.test_sent",
        outcome=Outcome.SUCCESS,
        subject=broadcast_subject(broadcast.id),
    )


class StepArgs(BaseModel):
    broadcast_id: int
    client_id: int


async def _pace() -> None:
    global _last_sent
    wait = SEND_INTERVAL.total_seconds() - (time.monotonic() - _last_sent)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_sent = time.monotonic()


def _finish_recipient(
    recipient: BroadcastRecipient, state: DeliveryState, error: str | None
) -> None:
    recipient.state = state
    recipient.sent_at = datetime.now(UTC)
    recipient.error = error[:_ERROR_LIMIT] if error else None


@task("broadcasts.send", StepArgs, needs_attention=False)
async def send_step(context: TaskContext, args: StepArgs) -> None:
    session = context.session
    # Разделяемая блокировка: остановка (11.7) дождётся отправки, которая уже идёт
    state = await session.scalar(
        select(Broadcast.state).where(Broadcast.id == args.broadcast_id).with_for_update(read=True)
    )
    if state != BroadcastState.SENDING:
        return
    recipient = await session.scalar(
        select(BroadcastRecipient)
        .where(
            BroadcastRecipient.broadcast_id == args.broadcast_id,
            BroadcastRecipient.client_id == args.client_id,
        )
        .with_for_update()
    )
    if recipient is None or recipient.state != DeliveryState.PENDING:
        await _continue(session, args)
        return
    if context.previous_result not in _SAFE_TO_SEND:
        # Прошлая попытка оборвалась посреди отправки: сообщение могло уйти (11.6)
        _finish_recipient(recipient, DeliveryState.FAILED, _ABORTED)
        await _continue(session, args)
        return
    if await due_tasks_exist(session, send_message.name):
        # Служебные сообщения — раньше рассылки (11.13)
        await send_step.enqueue(session, args, delay=YIELD_DELAY)
        return
    await _deliver(session, recipient)
    await _continue(session, args)


async def _deliver(session: AsyncSession, recipient: BroadcastRecipient) -> None:
    broadcast = await session.get_one(Broadcast, recipient.broadcast_id)
    client = await session.get_one(Client, recipient.client_id)
    chat_id = await _chat_id(session, client.id)
    if chat_id is None:
        _finish_recipient(recipient, DeliveryState.FAILED, "У клиента нет Telegram-аккаунта")
        return
    image = await session.get(BroadcastImage, broadcast.id)
    content = BroadcastContent.model_validate(broadcast.content)
    texts = await load_texts(session)
    message = build_message(content, texts, client.language_code, _photo(image))
    await _pace()
    try:
        file_id = await runtime.current().sender.send(chat_id, message)
    except RecipientBlockedError as error:
        # Пометка «бот заблокирован» снимается, когда клиент снова откроет бот (11.9)
        await mark_blocked(session, client.id)
        _finish_recipient(recipient, DeliveryState.BOT_BLOCKED, str(error))
    except NotAcceptedError as error:
        # Telegram точно не принял — повтор того же шага безопасен
        raise RejectedError(str(error)) from error
    except (UndeliverableError, AmbiguousDeliveryError) as error:
        _finish_recipient(recipient, DeliveryState.FAILED, str(error))
    else:
        _remember_file(image, file_id)
        _finish_recipient(recipient, DeliveryState.DELIVERED, None)


async def _chat_id(session: AsyncSession, client_id: int) -> int | None:
    return await session.scalar(
        select(TelegramAccount.telegram_id)
        .where(TelegramAccount.client_id == client_id)
        .order_by(TelegramAccount.created_at)
        .limit(1)
    )


@send_step.on_failed
async def _step_failed(session: AsyncSession, _task_id: int, args: StepArgs) -> None:
    """Telegram так и не принял сообщение: получатель — с ошибкой, рассылка идёт дальше."""
    await session.execute(
        update(BroadcastRecipient)
        .where(
            BroadcastRecipient.broadcast_id == args.broadcast_id,
            BroadcastRecipient.client_id == args.client_id,
            BroadcastRecipient.state == DeliveryState.PENDING,
        )
        .values(state=DeliveryState.FAILED, sent_at=func.now(), error=_NOT_ACCEPTED)
    )
    await _continue(session, args)


async def _continue(session: AsyncSession, args: StepArgs) -> None:
    """Шаг для следующего получателя, а если получателей не осталось — итог (11.8)."""
    await session.flush()
    following = await session.scalar(
        select(BroadcastRecipient.client_id)
        .where(
            BroadcastRecipient.broadcast_id == args.broadcast_id,
            BroadcastRecipient.client_id > args.client_id,
            BroadcastRecipient.state == DeliveryState.PENDING,
        )
        .order_by(BroadcastRecipient.client_id)
        .limit(1)
    )
    if following is not None:
        await send_step.enqueue(
            session, StepArgs(broadcast_id=args.broadcast_id, client_id=following)
        )
        return
    await finish(session, args.broadcast_id)


async def finish(session: AsyncSession, broadcast_id: int) -> None:
    """Рассылка дошла до конца: итог — в журнал (11.8, 11.11)."""
    finished = await session.scalar(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id, Broadcast.state == BroadcastState.SENDING)
        .values(state=BroadcastState.FINISHED, finished_at=func.now())
        .returning(Broadcast.id)
    )
    if finished is None:
        return
    totals = (await totals_of(session, [broadcast_id]))[broadcast_id]
    await record(
        session,
        actor=Actor.SYSTEM,
        action="broadcast.finished",
        outcome=Outcome.SUCCESS,
        subject=broadcast_subject(broadcast_id),
        details=totals.as_details(),
    )
