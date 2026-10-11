"""Сообщения клиентам и уведомления команде через Telegram.

Остальной код знает только этот интерфейс: `send_to_client` и `notify_team`
ставят сообщение в очередь в своей транзакции — откатилось действие, не уйдёт и
сообщение о нём. Отправка, повторы и недоставка — внутри модуля (4.33, сквозное
правило 4). Тексты — только по ключам (0018).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import TelegramAccount
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.messaging._dates import DATE_FORMAT, DateEntity, Dates, DateValue, utf16_length
from remnabay.messaging._sender import (
    AmbiguousDeliveryError,
    Button,
    DeliveryError,
    NotAcceptedError,
    OutgoingMessage,
    RecipientBlockedError,
    Sender,
    TelegramSender,
    UndeliverableError,
)
from remnabay.messaging._tasks import (
    ButtonArgs,
    SendArgs,
    load_texts,
    not_delivered,
    send_message,
)

# «Главное меню» — на каждом событии (03-screens); нажатие разбирает бот
MAIN_MENU_CALLBACK = "menu:main"
MAIN_MENU_BUTTON = ButtonArgs(text_key="btn.main_menu", callback_data=MAIN_MENU_CALLBACK)
# «Купить подписку» — ведёт к выбору тарифа (С9, С16)
BUY_CALLBACK = "buy"
# «Как подключить» — инструкция по подключению подписки (С2); `guide:<id подписки>`
GUIDE_CALLBACK_PREFIX = "guide"


def guide_button(subscription_id: int) -> ButtonArgs:
    """«Как подключить» на событии со ссылкой (С1, С3, С15)."""
    return ButtonArgs(
        text_key="btn.how_to_connect", callback_data=f"{GUIDE_CALLBACK_PREFIX}:{subscription_id}"
    )


def buy_button() -> ButtonArgs:
    """«Купить подписку» — целевое действие события (С16)."""
    return ButtonArgs(text_key="btn.buy", callback_data=BUY_CALLBACK, style="success")


async def send_to_client(
    session: AsyncSession,
    client_id: int,
    text_key: str,
    *,
    variables: dict[str, str] | None = None,
    dates: Mapping[str, datetime] | None = None,
    buttons: Sequence[ButtonArgs] = (),
) -> None:
    """Ставит сообщение клиенту в его Telegram-аккаунт. `dates` — переменные-даты:
    клиент видит их сущностью date_time (0044)."""
    chat_id = await session.scalar(
        select(TelegramAccount.telegram_id)
        .where(TelegramAccount.client_id == client_id)
        .order_by(TelegramAccount.created_at)
        .limit(1)
    )
    args = SendArgs(
        chat_id=chat_id or 0,
        client_id=client_id,
        text_key=text_key,
        variables=variables or {},
        dates=dict(dates or {}),
        buttons=list(buttons),
    )
    if chat_id is None:
        await not_delivered(session, args, "no_telegram")
        return
    await send_message.enqueue(session, args)


async def tell_action_failed(session: AsyncSession, client_id: int) -> None:
    """С17 «Не удалось выполнить действие»: команда отменила проваленную операцию
    клиента (4.32). Вызывает обработчик отмены операции (`on_cancelled`)."""
    await send_to_client(session, client_id, "event.action_failed", buttons=[MAIN_MENU_BUTTON])


async def notify_team(
    session: AsyncSession,
    text_key: str,
    *,
    variables: dict[str, str] | None = None,
    dates: Mapping[str, datetime] | None = None,
) -> None:
    """Уведомление команды: в MVP — владельцам в Telegram (04-operator-settings,
    «Уведомления команде»). Владелец из `.env` есть среди них — его гарантирует
    запуск магазина (0036)."""
    owners = await session.scalars(
        select(TeamMember.telegram_id)
        .where(TeamMember.role == TeamRole.OWNER, TeamMember.revoked_at.is_(None))
        .order_by(TeamMember.id)
    )
    for telegram_id in owners:
        await send_message.enqueue(
            session,
            SendArgs(
                chat_id=telegram_id,
                text_key=text_key,
                variables=variables or {},
                dates=dict(dates or {}),
            ),
        )


__all__ = [
    "BUY_CALLBACK",
    "DATE_FORMAT",
    "GUIDE_CALLBACK_PREFIX",
    "MAIN_MENU_BUTTON",
    "MAIN_MENU_CALLBACK",
    "AmbiguousDeliveryError",
    "Button",
    "ButtonArgs",
    "DateEntity",
    "DateValue",
    "Dates",
    "DeliveryError",
    "NotAcceptedError",
    "OutgoingMessage",
    "RecipientBlockedError",
    "SendArgs",
    "Sender",
    "TelegramSender",
    "UndeliverableError",
    "buy_button",
    "guide_button",
    "load_texts",
    "notify_team",
    "send_message",
    "send_to_client",
    "tell_action_failed",
    "utf16_length",
]
