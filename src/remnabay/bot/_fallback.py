"""Ответ на всё, что бот не понял (03-bot-texts.md, «Непонятное сообщение»).

Роутер подключается последним: остальные экраны появляются в блоках 3, 5, 6, 12.
"""

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from remnabay.bot._context import BotContext


async def unknown_message(message: Message, ctx: BotContext) -> None:
    await message.answer(ctx.render("fallback.unknown"))


async def unknown_button(query: CallbackQuery) -> None:
    # Кнопка устаревшего сообщения: убрать «часики» у кнопки и ничего не делать
    await query.answer()


def build_router() -> Router:
    """Новый роутер на каждый диспетчер: в aiogram роутер подключается только к одному."""
    router = Router(name="fallback")
    router.message.register(unknown_message)
    router.callback_query.register(unknown_button)
    return router
