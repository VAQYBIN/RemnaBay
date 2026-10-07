"""Контекст обновления бота: сессия базы, клиент, участник команды, тексты."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access import active_member, is_login_link
from remnabay.brand import brand_name
from remnabay.clients import TelegramUser, register_telegram_user
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.team import TeamMember
from remnabay.messaging import load_texts
from remnabay.shop import ShopState, is_tester, shop_state
from remnabay.texts import Texts

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# Ключ контекста в данных обработчика aiogram: обработчик объявляет `ctx: BotContext`
CONTEXT_KEY = "ctx"
SESSIONS_KEY = "sessions"


@dataclass(frozen=True)
class BotContext:
    session: AsyncSession
    bot: Bot
    client: Client
    # Действующий участник команды, если человек в ней состоит
    member: TeamMember | None
    texts: Texts
    # Клиент появился этим обновлением — впервые открыл бот (3.15)
    new_client: bool = False

    def render(self, key: str, /, **variables: str) -> str:
        """Текст по ключу на языке клиента (0018)."""
        return self.texts.render(key, self.client.language_code, **variables)

    async def brand_name(self) -> str:
        """`{brand_name}` в текстах бота. Пока оператор не задал название, — имя бота
        в Telegram: это тоже бренд оператора, а RemnaBay клиент видеть не должен (0038)."""
        return await brand_name(self.session) or (await self.bot.me()).first_name


async def context_middleware(handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
    """Каждое обновление из личного чата — в своей транзакции; клиент учитывается сразу.

    Обновления не из личного чата (группы, каналы) бот не обрабатывает.
    """
    user: User | None = data.get("event_from_user")
    chat: Chat | None = data.get("event_chat")
    if user is None or user.is_bot or (chat is not None and chat.type != "private"):
        return None

    sessions: SessionFactory = data[SESSIONS_KEY]
    async with sessions() as session:
        new_client = await session.get(TelegramAccount, user.id) is None
        client = await register_telegram_user(
            session,
            TelegramUser(
                id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code,
            ),
            now=datetime.now(UTC),
        )
        ctx = BotContext(
            session=session,
            bot=data["bot"],
            client=client,
            member=await active_member(session, user.id),
            texts=await load_texts(session),
            new_client=new_client,
        )
        if await _shop_hidden_from(ctx, event, user.id):
            await _answer_shop_not_opened(event, ctx)
            await session.commit()
            return None
        data[CONTEXT_KEY] = ctx
        result = await handler(event, data)
        await session.commit()
        return result


async def _shop_hidden_from(ctx: BotContext, event: TelegramObject, telegram_id: int) -> bool:
    """Пока магазин не открыт, бот работает только для команды и тестировщиков (1.13, 1.21).

    Ссылку входа в админку бот разбирает всегда: попытку входа чужого аккаунта
    нужно отклонить и показать отказ на странице входа (1.6).
    """
    if ctx.member is not None or await shop_state(ctx.session) != ShopState.NOT_OPENED:
        return False
    if isinstance(event, Update) and event.message and is_login_link(event.message.text):
        return False
    return not await is_tester(ctx.session, telegram_id)


async def _answer_shop_not_opened(event: TelegramObject, ctx: BotContext) -> None:
    """«Магазин скоро откроется» на любое сообщение и нажатие (1.13)."""
    if not isinstance(event, Update):
        return
    text = ctx.render("shop.closed", brand_name=await ctx.brand_name())
    if isinstance(event.message, Message):
        await event.message.answer(text)
    elif isinstance(event.callback_query, CallbackQuery):
        await event.callback_query.answer()
        await ctx.bot.send_message(event.callback_query.from_user.id, text)
