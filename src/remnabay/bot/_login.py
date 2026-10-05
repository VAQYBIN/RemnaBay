"""Вход в админку из бота (1.4–1.6, решение 0051).

- Ссылка `t.me/<бот>?start=login_…` со страницы входа: участнику команды бот
  присылает подтверждение с кодом; остальным отвечает как на непонятное сообщение.
- Скрытая команда `/admin`: участнику — кнопка `login_url`, остальным — как на
  непонятное сообщение.
"""

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LoginUrl,
    Message,
)

from remnabay.access import (
    START_PREFIX,
    LoginClosed,
    LoginOpened,
    confirm_bot_login,
    open_bot_login,
)
from remnabay.bot._context import BotContext

ADMIN_COMMAND = "admin"
# Адрес, куда кнопка login_url передаёт подписанные данные Telegram
ADMIN_LOGIN_URL_KEY = "admin_login_url"


class LoginConfirm(CallbackData, prefix="login"):
    request_id: int


async def open_login(message: Message, command: CommandObject, ctx: BotContext) -> None:
    if message.from_user is None or command.args is None:
        return
    opened = await open_bot_login(
        ctx.session, command.args, message.from_user.id, now=datetime.now(UTC)
    )
    match opened:
        case LoginOpened(request_id=request_id, code=code):
            button = InlineKeyboardButton(
                text=ctx.render("btn.login_confirm"),
                callback_data=LoginConfirm(request_id=request_id).pack(),
            )
            await ctx.session.commit()
            await message.answer(
                ctx.render("team.login.confirm", brand_name=await ctx.brand_name(), code=code),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
            )
        case LoginClosed.EXPIRED if ctx.member is not None:
            await message.answer(ctx.render("team.login.expired"))
        case _:
            # Аккаунт не из команды: ссылка входа для него — непонятное сообщение (1.6)
            await ctx.session.commit()
            await message.answer(ctx.render("fallback.unknown"))


async def confirm_login(query: CallbackQuery, callback_data: LoginConfirm, ctx: BotContext) -> None:
    confirmed = await confirm_bot_login(
        ctx.session, callback_data.request_id, query.from_user.id, now=datetime.now(UTC)
    )
    await ctx.session.commit()
    text = ctx.render("team.login.confirmed" if confirmed else "team.login.expired")
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=None)


async def admin_command(message: Message, ctx: BotContext, admin_login_url: str) -> None:
    if ctx.member is None:
        await message.answer(ctx.render("fallback.unknown"))
        return
    button = InlineKeyboardButton(
        text=ctx.render("btn.open_admin"), login_url=LoginUrl(url=admin_login_url)
    )
    await message.answer(
        ctx.render("team.admin_link", brand_name=await ctx.brand_name()),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


def build_router() -> Router:
    router = Router(name="login")
    router.message.register(
        open_login, CommandStart(deep_link=True, magic=F.args.startswith(START_PREFIX))
    )
    router.message.register(admin_command, Command(ADMIN_COMMAND))
    router.callback_query.register(confirm_login, LoginConfirm.filter())
    return router
