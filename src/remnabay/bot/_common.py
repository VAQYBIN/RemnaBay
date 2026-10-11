"""Общее для экранов бота: кнопки-нажатия, показ экрана, поддержка, закрытый магазин."""

import asyncio
from datetime import timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InaccessibleMessage

from remnabay.bot._context import BotContext
from remnabay.bot._screens import Keyboard, Screen
from remnabay.devices import HWID_ENABLED, DeviceList, list_devices, remember_device_limit
from remnabay.domain.subscriptions import Subscription
from remnabay.messaging import BUY_CALLBACK, GUIDE_CALLBACK_PREFIX
from remnabay.panel import PanelError
from remnabay.payments import live_subscriptions
from remnabay.shop import SHOP_SUPPORT_CONTACT, is_tester, sales_open
from remnabay.shop_settings import get_setting

# «Купить подписку», «Мои подписки», «Назад» к меню (правка того же сообщения)
BUY = BUY_CALLBACK
SUBSCRIPTIONS = "subs"
# «Попробовать бесплатно» (5.2, 5.4)
TRY_FREE = "trial"
BACK_TO_MENU = "menu:back"
# Сколько ждать панель, пока клиент смотрит на экран: дальше — последние известные данные
PANEL_TIMEOUT = timedelta(seconds=5)


class Card(CallbackData, prefix="sub"):
    subscription_id: int


class Guide(CallbackData, prefix=GUIDE_CALLBACK_PREFIX):
    """«Как подключить» — инструкция по подключению подписки (С2)."""

    subscription_id: int


class GuideApps(CallbackData, prefix="apps"):
    """«Другие приложения» платформы в инструкции (12.3)."""

    subscription_id: int
    platform: str


class DevicesScreen(CallbackData, prefix="devs"):
    """З7: устройства подписки (6.2)."""

    subscription_id: int


class RemoveDevice(CallbackData, prefix="devdel"):
    """«Удалить» у устройства: отпечаток HWID — данные кнопки ограничены 64 байтами."""

    subscription_id: int
    device: str


class AskRemoveAll(CallbackData, prefix="devall"):
    """«Удалить все» — сначала подтверждение (6.5)."""

    subscription_id: int


class RemoveAll(CallbackData, prefix="devallok"):
    """«Да, удалить все» (6.5)."""

    subscription_id: int


async def support_url(ctx: BotContext) -> str | None:
    """Контакт поддержки как ссылка: «@имя», «t.me/имя» или адрес сайта."""
    contact = (await get_setting(ctx.session, SHOP_SUPPORT_CONTACT)).strip()
    if contact.startswith("@") and len(contact) > 1:
        return f"https://t.me/{contact[1:]}"
    if contact.startswith(("https://", "http://")):
        return contact
    if contact.startswith("t.me/"):
        return f"https://{contact}"
    return None


async def sales_allowed(ctx: BotContext, telegram_id: int) -> bool:
    """Покупка, продление и триал: в открытом магазине; для команды и тестировщиков —
    всегда (1.22)."""
    if await sales_open(ctx.session) or ctx.member is not None:
        return True
    return await is_tester(ctx.session, telegram_id)


async def paused_screen(ctx: BotContext) -> Screen:
    """З0а: «Магазин временно закрыт» вместо покупки, продления и триала (1.22)."""
    text = ctx.render("shop.paused", brand_name=await ctx.brand_name())
    keyboard = Keyboard().action(ctx.render("btn.main_menu"), BACK_TO_MENU)
    return Screen.plain(text, keyboard)


async def show(query: CallbackQuery, ctx: BotContext, screen: Screen) -> None:
    """Экран вместо текущего сообщения меню."""
    await query.answer()
    message = query.message
    if message is None or isinstance(message, InaccessibleMessage):
        await screen.send(ctx.bot, query.from_user.id)
        return
    await screen.edit(ctx.bot, query.from_user.id, message.message_id)


async def own_subscription(ctx: BotContext, subscription_id: int) -> Subscription | None:
    for subscription in await live_subscriptions(ctx.session, ctx.client.id):
        if subscription.id == subscription_id:
            return subscription
    return None


async def panel_devices(ctx: BotContext, subscription: Subscription) -> DeviceList | None:
    """Устройства подписки из панели (6.2, 6.3). `None` — лимит устройств в панели
    выключен (6.1). Панель не ответила — `PanelError` или `TimeoutError`.

    Таймаут — только на запросы к панели: запись в базу не прерывается."""
    seconds = PANEL_TIMEOUT.total_seconds()
    async with asyncio.timeout(seconds):
        settings = await ctx.panel.get_subscription_settings()
    limit = await remember_device_limit(ctx.session, settings)
    if not limit.enabled:
        return None
    async with asyncio.timeout(seconds):
        return await list_devices(ctx.panel, subscription, limit)


async def card_devices(ctx: BotContext, subscription: Subscription) -> DeviceList | None:
    """Устройства для карточки: «занято X из Y» и кнопка раздела (З3, 6.1). `None` —
    лимит устройств в панели выключен. Панель не ответила — список пустой, без лимита,
    а кнопка раздела — по последнему ответу панели."""
    try:
        return await panel_devices(ctx, subscription)
    except PanelError, TimeoutError:
        known = await get_setting(ctx.session, HWID_ENABLED)
        return DeviceList(devices=[], limit=None) if known else None
