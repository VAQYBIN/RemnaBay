"""/help — инструкция по подключению и контакт поддержки (03-screens, «Команды»; решение 0058).

- Одна подписка — `help.body` и «Как подключить».
- Несколько — `help.body` и по кнопке на каждую подписку с её названием.
- Подписок нет — `help.no_subscription` и «Купить подписку»; если триал доступен
  клиенту (5.3) — ещё «Попробовать бесплатно».
Без контакта поддержки — варианты текста `.no_support`.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from remnabay.bot._common import BUY, TRY_FREE, Guide
from remnabay.bot._context import BotContext
from remnabay.bot._screens import SUCCESS, Keyboard, Screen
from remnabay.payments import live_subscriptions
from remnabay.shop import SHOP_SUPPORT_CONTACT
from remnabay.shop_settings import get_setting
from remnabay.trial import trial_available


async def help_screen(ctx: BotContext) -> Screen:
    contact = (await get_setting(ctx.session, SHOP_SUPPORT_CONTACT)).strip()
    subscriptions = await live_subscriptions(ctx.session, ctx.client.id)
    keyboard = Keyboard()
    if not subscriptions:
        key = "help.no_subscription"
        trial = await trial_available(ctx.session, ctx.client)
        if trial:
            keyboard.action(ctx.render("btn.try_free"), TRY_FREE, style=SUCCESS)
        keyboard.action(ctx.render("btn.buy"), BUY, style=None if trial else SUCCESS)
    else:
        key = "help.body"
        if len(subscriptions) == 1:
            guide = Guide(subscription_id=subscriptions[0].id).pack()
            keyboard.action(ctx.render("btn.how_to_connect"), guide)
        else:
            for subscription in subscriptions:
                keyboard.action(subscription.name, Guide(subscription_id=subscription.id).pack())
    text = ctx.render(key, support_contact=contact) if contact else ctx.render(f"{key}.no_support")
    return Screen.plain(text, keyboard)


async def help_command(message: Message, ctx: BotContext) -> None:
    await (await help_screen(ctx)).send(ctx.bot, message.chat.id)


def build_router() -> Router:
    router = Router(name="help")
    router.message.register(help_command, Command("help"))
    return router
