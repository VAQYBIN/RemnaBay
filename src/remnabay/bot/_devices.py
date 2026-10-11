"""З7. Устройства подписки (блок 6, сценарий «Управление устройствами»).

Раздел есть, только если в панели включён лимит устройств (6.1). Список и лимит
читаются из панели при открытии (6.2, 6.3); у каждой подписки — свои устройства
(6.7). Удаление — через панель, с записью в журнал (6.4); удаление всех — после
подтверждения (6.5); панель недоступна — удаление в очереди (6.6).
"""

import asyncio

from aiogram import Router
from aiogram.types import CallbackQuery

from remnabay.bot._common import (
    PANEL_TIMEOUT,
    AskRemoveAll,
    Card,
    DevicesScreen,
    RemoveAll,
    RemoveDevice,
    own_subscription,
    show,
)
from remnabay.bot._context import BotContext
from remnabay.bot._screens import DANGER, Dates, Keyboard, Screen, date_value
from remnabay.bot._shop import main_menu, subscription_card
from remnabay.devices import (
    DeviceList,
    Removal,
    app_name,
    device_limit,
    handle,
    list_devices,
    remove_devices,
)
from remnabay.domain.subscriptions import Subscription
from remnabay.panel import Device, PanelError

# Неизвестное панели значение в строке устройства (решение 0059)
UNKNOWN = "—"


async def _devices(ctx: BotContext, subscription: Subscription) -> DeviceList | None:
    """Устройства из панели. `None` — лимит устройств в панели выключен (6.1).
    Панель не ответила — `PanelError` или `TimeoutError` у вызывающего."""
    async with asyncio.timeout(PANEL_TIMEOUT.total_seconds()):
        limit = await device_limit(ctx.session, ctx.panel)
        if not limit.enabled:
            return None
        return await list_devices(ctx.panel, subscription, limit)


def _names(device: Device) -> dict[str, str]:
    return {
        "platform": (device.platform or "").strip() or UNKNOWN,
        "model": (device.device_model or "").strip() or UNKNOWN,
        "app": app_name(device) or UNKNOWN,
    }


async def _list_screen(
    ctx: BotContext, subscription: Subscription, devices: DeviceList, note: str | None
) -> Screen:
    """Платформа, модель, приложение, дата подключения; «занято X из Y» (6.2)."""
    dates = Dates()
    limit = str(devices.limit) if devices.limit is not None else UNKNOWN
    header = ctx.render(
        "devices.header", subscription_name=subscription.name, used=str(devices.used), limit=limit
    )
    rows = [
        ctx.render(
            "devices.row",
            connected_at=dates.mark(await date_value(ctx, device.created_at)),
            **_names(device),
        )
        for device in devices.devices
    ]
    parts = [ctx.render(note)] if note is not None else []
    parts += [header, "\n".join(rows) if rows else ctx.render("devices.empty")]
    keyboard = Keyboard()
    for device in devices.devices:
        names = _names(device)
        label = ctx.render(
            "devices.button_delete", platform=names["platform"], model=names["model"]
        )
        remove = RemoveDevice(subscription_id=subscription.id, device=handle(device.hwid))
        keyboard.action(label, remove.pack(), style=DANGER)
    if devices.devices:
        keyboard.action(
            ctx.render("btn.delete_all"),
            AskRemoveAll(subscription_id=subscription.id).pack(),
            style=DANGER,
        )
    keyboard.action(ctx.render("btn.back"), Card(subscription_id=subscription.id).pack())
    return Screen.plain("\n\n".join(parts), keyboard, dates)


async def devices_screen(
    ctx: BotContext, subscription: Subscription, note: str | None = None
) -> Screen:
    """З7. Лимит в панели выключили — карточка подписки, без раздела (6.1). Панель
    не отвечает — «данные могут быть неактуальны» (4.12): список — только из панели."""
    try:
        devices = await _devices(ctx, subscription)
    except PanelError, TimeoutError:
        keyboard = Keyboard().action(
            ctx.render("btn.back"), Card(subscription_id=subscription.id).pack()
        )
        text = ctx.render("menu.stale_data")
        return Screen.plain(f"{ctx.render(note)}\n\n{text}" if note else text, keyboard)
    if devices is None:
        return await subscription_card(ctx, subscription)
    return await _list_screen(ctx, subscription, devices, note)


async def open_devices(query: CallbackQuery, callback_data: DevicesScreen, ctx: BotContext) -> None:
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await devices_screen(ctx, subscription))


async def _remove(
    query: CallbackQuery, ctx: BotContext, subscription: Subscription, hwid: str | None
) -> None:
    try:
        removal = await remove_devices(ctx.session, ctx.panel, subscription, hwid)
    except PanelError:
        # Панель отказала (например, устройства уже нет) — показываем, что в ней сейчас
        await show(query, ctx, await devices_screen(ctx, subscription))
        return
    if removal == Removal.QUEUED:
        # 6.6: удаление выполнится, как только панель станет доступна
        await show(query, ctx, await devices_screen(ctx, subscription, "devices.queued"))
        return
    await show(query, ctx, await devices_screen(ctx, subscription, "devices.deleted"))


async def remove_one(query: CallbackQuery, callback_data: RemoveDevice, ctx: BotContext) -> None:
    """«Удалить» у устройства (6.4)."""
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await show(query, ctx, await main_menu(ctx))
        return
    try:
        devices = await _devices(ctx, subscription)
    except PanelError, TimeoutError:
        devices = None
    device = devices.find(callback_data.device) if devices is not None else None
    if device is None:
        # Устройства уже нет или панель не ответила — актуальный список
        await show(query, ctx, await devices_screen(ctx, subscription))
        return
    await _remove(query, ctx, subscription, device.hwid)


async def ask_remove_all(
    query: CallbackQuery, callback_data: AskRemoveAll, ctx: BotContext
) -> None:
    """«Удалить все» — сначала подтверждение (6.5)."""
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await show(query, ctx, await main_menu(ctx))
        return
    keyboard = (
        Keyboard()
        .action(
            ctx.render("btn.confirm_delete_all"),
            RemoveAll(subscription_id=subscription.id).pack(),
            style=DANGER,
        )
        .action(ctx.render("btn.back"), DevicesScreen(subscription_id=subscription.id).pack())
    )
    await show(query, ctx, Screen.plain(ctx.render("devices.confirm_delete_all"), keyboard))


async def remove_all(query: CallbackQuery, callback_data: RemoveAll, ctx: BotContext) -> None:
    """«Да, удалить все» (6.5)."""
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await show(query, ctx, await main_menu(ctx))
        return
    await _remove(query, ctx, subscription, None)


def build_router() -> Router:
    router = Router(name="devices")
    router.callback_query.register(open_devices, DevicesScreen.filter())
    router.callback_query.register(remove_one, RemoveDevice.filter())
    router.callback_query.register(ask_remove_all, AskRemoveAll.filter())
    router.callback_query.register(remove_all, RemoveAll.filter())
    return router
