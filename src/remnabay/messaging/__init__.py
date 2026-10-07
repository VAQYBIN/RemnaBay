"""Сообщения клиентам и уведомления команде через Telegram.

Остальной код знает только этот интерфейс: `send_to_client` и `notify_team`
ставят сообщение в очередь в своей транзакции — откатилось действие, не уйдёт и
сообщение о нём. Отправка, повторы и недоставка — внутри модуля (4.33, сквозное
правило 4). Тексты — только по ключам (0018).
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import TelegramAccount
from remnabay.domain.team import TeamMember, TeamRole
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


async def send_to_client(
    session: AsyncSession,
    client_id: int,
    text_key: str,
    *,
    variables: dict[str, str] | None = None,
    buttons: Sequence[ButtonArgs] = (),
) -> None:
    """Ставит сообщение клиенту в его Telegram-аккаунт."""
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
    session: AsyncSession, text_key: str, *, variables: dict[str, str] | None = None
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
            SendArgs(chat_id=telegram_id, text_key=text_key, variables=variables or {}),
        )


__all__ = [
    "MAIN_MENU_BUTTON",
    "MAIN_MENU_CALLBACK",
    "AmbiguousDeliveryError",
    "Button",
    "ButtonArgs",
    "DeliveryError",
    "NotAcceptedError",
    "OutgoingMessage",
    "RecipientBlockedError",
    "SendArgs",
    "Sender",
    "TelegramSender",
    "UndeliverableError",
    "load_texts",
    "notify_team",
    "send_message",
    "send_to_client",
    "tell_action_failed",
]
