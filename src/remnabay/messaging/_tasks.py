"""Сообщения клиентам и команде — операции очереди (4.33, сквозное правило 4).

Текст собирается в момент отправки: по ключу, на языке клиента, с правками
оператора (0018). Сообщение отправляется не больше одного раза: повтор — только
если прошлая попытка точно его не отправила. Недоставка — не ошибка: действие,
о котором сообщение, уже выполнено; недоставка пишется в журнал.
"""

import functools
import logging

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.clients import Client
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging._sender import (
    AmbiguousDeliveryError,
    Button,
    NotAcceptedError,
    OutgoingMessage,
    RecipientBlockedError,
    UndeliverableError,
)
from remnabay.queue import AttemptResult, RejectedError, TaskContext, task
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting
from remnabay.texts import Catalog, TextError, Texts, load_default_catalogs, load_overrides

logger = logging.getLogger(__name__)

CLIENT_SUBJECT = "client"
CHAT_SUBJECT = "telegram_chat"
# Прошлая попытка точно не отправила сообщение: её не было или Telegram отказал
_SAFE_TO_SEND = (None, AttemptResult.REJECTED)


class ButtonArgs(BaseModel):
    """Кнопка: подпись по ключу текста, ссылка или текст для копирования."""

    text_key: str
    url: str | None = None
    copy_text: str | None = None


class SendArgs(BaseModel):
    chat_id: int
    # Клиент, если сообщение ему: язык, пометка «бот заблокирован», журнал
    client_id: int | None = None
    text_key: str
    variables: dict[str, str] = {}
    buttons: list[ButtonArgs] = []


@functools.cache
def _catalogs() -> dict[str, Catalog]:
    return load_default_catalogs()


def _subject(args: SendArgs) -> Subject:
    if args.client_id is not None:
        return Subject(CLIENT_SUBJECT, args.client_id)
    return Subject(CHAT_SUBJECT, args.chat_id)


async def not_delivered(
    session: AsyncSession, args: SendArgs, reason: str, error: str | None = None
) -> None:
    """Недоставка сообщения — в журнал (сквозное правило 4)."""
    await record(
        session,
        actor=Actor.SYSTEM,
        action="message.not_delivered",
        outcome=Outcome.FAILURE,
        subject=_subject(args),
        details={"key": args.text_key, "reason": reason, "error": error},
    )


async def load_texts(session: AsyncSession) -> Texts:
    """Тексты бота: по умолчанию, с правками оператора, язык по умолчанию — из настроек."""
    return Texts(
        _catalogs(),
        await load_overrides(session),
        default_language=await get_setting(session, SHOP_LANGUAGE),
    )


async def _render(session: AsyncSession, args: SendArgs) -> OutgoingMessage:
    language = None
    if args.client_id is not None:
        language = await session.scalar(
            select(Client.language_code).where(Client.id == args.client_id)
        )
    texts = await load_texts(session)
    buttons = tuple(
        Button(texts.render(button.text_key, language), url=button.url, copy_text=button.copy_text)
        for button in args.buttons
    )
    return OutgoingMessage(texts.render(args.text_key, language, **args.variables), buttons)


async def _mark_blocked(session: AsyncSession, client_id: int) -> None:
    """Пометка «бот заблокирован»; снимается, когда клиент снова открывает бот (11.9)."""
    await session.execute(
        update(Client)
        .where(Client.id == client_id, Client.bot_blocked_at.is_(None))
        .values(bot_blocked_at=func.now())
    )


@task("messages.send", SendArgs)
async def send_message(context: TaskContext, args: SendArgs) -> None:
    session = context.session
    try:
        message = await _render(session, args)
    except TextError as error:
        logger.exception("Сообщение %s не собрано", args.text_key)
        await not_delivered(session, args, "text_error", str(error))
        return
    if context.previous_result not in _SAFE_TO_SEND:
        # Прошлая попытка оборвалась посреди отправки: сообщение могло уйти (4.33)
        await not_delivered(session, args, "ambiguous", "прошлая попытка оборвалась")
        return
    try:
        await runtime.current().sender.send(args.chat_id, message)
    except RecipientBlockedError as error:
        if args.client_id is not None:
            await _mark_blocked(session, args.client_id)
        await not_delivered(session, args, "blocked", str(error))
    except UndeliverableError as error:
        await not_delivered(session, args, "rejected", str(error))
    except AmbiguousDeliveryError as error:
        await not_delivered(session, args, "ambiguous", str(error))
    except NotAcceptedError as error:
        raise RejectedError(str(error)) from error


@send_message.on_failed
async def _send_failed(session: AsyncSession, _task_id: int, args: SendArgs) -> None:
    # Telegram так и не принял сообщение: недоставка, а не уведомление команды —
    # иначе сбой Telegram порождал бы новые сообщения
    await not_delivered(session, args, "not_accepted")
