"""Клиент в боте: главное меню, подписки, выбор тарифа, подтверждение, оплата (блок 3).

Экраны З1–З6 (03-screens). Покупка и продление закрыты, пока магазин временно
закрыт (1.22): клиент видит «Магазин временно закрыт», а свои подписки — как
обычно. Для команды и тестировщиков бот работает как обычно (1.21).
"""

import math
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InputRichBlockDetails,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichBlockUnion,
    InputRichMessage,
    Message,
    RichBlockTableCell,
    RichMessageButton,
    RichTextBold,
    RichTextUnion,
)
from babel.numbers import format_currency, format_decimal

from remnabay.bot._common import (
    BACK_TO_MENU,
    BUY,
    PROMO_MENU,
    SUBSCRIPTIONS,
    TRY_FREE,
    Card,
    DevicesScreen,
    GuideBack,
    GuideHere,
    PromoAsk,
    card_devices,
    own_subscription,
    paused_screen,
    sales_allowed,
    show,
    support_url,
)
from remnabay.bot._context import BotContext
from remnabay.bot._screens import (
    SUCCESS,
    Dates,
    Keyboard,
    Screen,
    button_rows,
    date_value,
    rich_action,
    rich_copy,
    rich_date,
    rich_link,
    rich_relative,
)
from remnabay.broadcasts import BUY_CALLBACK as BROADCAST_BUY
from remnabay.broadcasts import RENEW_CALLBACK as BROADCAST_RENEW
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.promo import PromoCode
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_CALLBACK, RENEW_CALLBACK_PREFIX
from remnabay.panel_sync import panel_available
from remnabay.payments import (
    CatalogError,
    Checkout,
    CheckoutError,
    CheckoutPromo,
    PaymentUnavailableError,
    PriceChangedError,
    Providers,
    can_purchase,
    can_renew,
    cancel_by_client,
    change_tariffs,
    check_choice,
    disabled_in_panel,
    is_active,
    live_subscriptions,
    open_invoice,
    quote,
    renewal_tariff,
    tariffs_on_sale,
    trial_for_purchase,
)
from remnabay.promo import (
    PromoRejectedError,
    accept,
    check_discount,
    is_accepted,
    still_usable,
)
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import get_setting
from remnabay.trial import GIGABYTE, request_trial, trial_available

# Ключ данных обработчика: провайдеры с ключами оператора (3.27)
PROVIDERS_KEY = "providers"
_KOPECKS = 100
# Кнопок в ряду внутри rich-сообщения: действия карточки и тарифы (0060)
_CARD_ROW = 2
_TARIFF_ROW = 3


class Renew(CallbackData, prefix=RENEW_CALLBACK_PREFIX):
    """«Продлить» — в карточке подписки и в сообщениях о сроке (С7, С8)."""

    subscription_id: int


class ChangeTariff(CallbackData, prefix="chtariff"):
    """«Сменить тариф» в карточке истёкшей подписки (3.41)."""

    subscription_id: int


class Pick(CallbackData, prefix="pick"):
    """Выбор тарифа: покупка (`subscription_id` = 0) или продление подписки."""

    tariff_id: int
    subscription_id: int


class Pay(CallbackData, prefix="pay"):
    """«Оплатить» на экране подтверждения: итог, который видел клиент, токен экрана
    (3.4, 3.5) и промокод на скидку (8.4; 0 — без промокода)."""

    tariff_id: int
    subscription_id: int
    price: int
    token: str
    promo: int = 0


class CancelPay(CallbackData, prefix="unpay"):
    payment_id: int


class RetryPay(CallbackData, prefix="payretry"):
    """«Попробовать снова» на сообщении «Платёж не прошёл» (3.12)."""

    payment_id: int


# --- Общее ---


def _now() -> datetime:
    return datetime.now(UTC)


async def _money(ctx: BotContext, amount: Decimal) -> str:
    language = ctx.texts.language_for(ctx.client.language_code)
    return format_currency(amount, await get_setting(ctx.session, SHOP_CURRENCY), locale=language)


def _days(ctx: BotContext, days: int) -> str:
    """Срок тарифа — всегда днями (решение 0056)."""
    return ctx.texts.quantity("unit.days", ctx.client.language_code, days)


def _state(subscription: Subscription, now: datetime) -> str:
    """Состояние подписки для клиента — по данным панели (01-domain)."""
    if subscription.panel_status == PanelUserStatus.DISABLED:
        return "disabled"
    if subscription.panel_status == PanelUserStatus.LIMITED:
        return "limited"
    if subscription.panel_status == PanelUserStatus.EXPIRED or not is_active(subscription, now):
        return "expired"
    return "active"


def _days_left(ctx: BotContext, subscription: Subscription, now: datetime) -> str:
    """Сколько осталось — полных или начатых дней: «3 дня» (0060)."""
    if subscription.expires_at is None or subscription.expires_at <= now:
        return ""
    days = math.ceil((subscription.expires_at - now) / timedelta(days=1))
    return ctx.texts.quantity("unit.days", ctx.client.language_code, days)


async def _summary(
    ctx: BotContext, subscription: Subscription, dates: Dates | None, now: datetime
) -> str:
    """Строка сводки: название, статус, дата окончания, сколько осталось (З1, З2, 0060)."""
    state = _state(subscription, now)
    end_date = ""
    if state in ("active", "expired") and subscription.expires_at is not None:
        value = await date_value(ctx, subscription.expires_at)
        end_date = dates.mark(value) if dates is not None else value.fallback
    return ctx.render(
        f"summary.{state}",
        subscription_name=subscription.name,
        end_date=end_date,
        days_text=_days_left(ctx, subscription, now),
    )


# --- З1. Главное меню ---


async def main_menu(ctx: BotContext) -> Screen:
    now = _now()
    subscriptions = await live_subscriptions(ctx.session, ctx.client.id)
    brand = await ctx.brand_name()
    dates = Dates()
    if not subscriptions:
        text = ctx.render(
            "menu.header.new", brand_name=brand, welcome_text=ctx.render("welcome_text")
        )
    else:
        lines = [await _summary(ctx, s, dates, now) for s in subscriptions]
        # Усыновлённый клиент впервые открыл бот: подписки уже здесь (сценарий
        # «Первое открытие бота усыновлённым клиентом»)
        header = "menu.header.adopted" if ctx.adopted_first_open else "menu.header.subscriptions"
        text = ctx.render(header, brand_name=brand, subscriptions_summary="\n".join(lines))
        if not await panel_available(ctx.session):
            # Панель недоступна: последние известные данные с пометкой (4.12)
            text = f"{text}\n\n{ctx.render('menu.stale_data')}"
    keyboard = Keyboard()
    if len(subscriptions) == 1:
        keyboard.action(
            ctx.render("btn.my_subscription"), Card(subscription_id=subscriptions[0].id).pack()
        )
    elif subscriptions:
        keyboard.action(ctx.render("btn.my_subscriptions"), SUBSCRIPTIONS)
    if any(can_renew(s) for s in subscriptions):
        # «Продлить» — к продлению единственной подписки или к списку подписок (3.40)
        target = (
            Renew(subscription_id=subscriptions[0].id).pack()
            if len(subscriptions) == 1
            else SUBSCRIPTIONS
        )
        keyboard.action(ctx.render("btn.renew"), target, style=SUCCESS)
    trial = await trial_available(ctx.session, ctx.client)
    if trial:
        # Триал доступен — целевое действие «Попробовать бесплатно» (5.2, З1)
        keyboard.action(ctx.render("btn.try_free"), TRY_FREE, style=SUCCESS)
    if can_purchase(subscriptions):
        # Без подписок или с единственной триальной — «Купить подписку» (3.36)
        keyboard.action(ctx.render("btn.buy"), BUY, style=None if trial else SUCCESS)
    # «Ввести промокод» — всегда (З1): промокод на дни работает и в закрытом магазине (1.22)
    keyboard.action(ctx.render("btn.promo"), PROMO_MENU)
    support = await support_url(ctx)
    if support is not None:
        keyboard.link(ctx.render("btn.support"), support)
    return Screen.plain(text, keyboard, dates)


async def start(message: Message, command: CommandObject, ctx: BotContext) -> None:
    """/start — главное меню; с параметром — реферальная ссылка (3.15)."""
    if command.args and ctx.new_client:
        await _link_referral(ctx, command.args)
    await (await main_menu(ctx)).send(ctx.bot, message.chat.id)


async def _link_referral(ctx: BotContext, code: str) -> None:
    """Реферальная связь — только у нового клиента и не на самого себя (3.15)."""
    referral = await ctx.session.get(ReferralCode, code)
    if referral is None or referral.client_id == ctx.client.id:
        return
    ctx.session.add(ReferralLink(invited_id=ctx.client.id, inviter_id=referral.client_id))
    await ctx.session.flush()
    await record(
        ctx.session,
        actor=Actor.client(ctx.client.id),
        action="referral.linked",
        outcome=Outcome.SUCCESS,
        subject=Subject("client", ctx.client.id),
        details={"inviter_id": referral.client_id, "code": code},
    )


async def menu_again(query: CallbackQuery, ctx: BotContext) -> None:
    """«Главное меню» на событии — меню новым сообщением: событие не перезаписывается."""
    await query.answer()
    await (await main_menu(ctx)).send(ctx.bot, query.from_user.id)


async def back_to_menu(query: CallbackQuery, ctx: BotContext) -> None:
    await show(query, ctx, await main_menu(ctx))


# --- З2, З3. Подписки ---


async def subscriptions_screen(ctx: BotContext) -> Screen:
    """З2: каждая подписка — отдельной кнопкой с названием и статусом (3.24)."""
    now = _now()
    keyboard = Keyboard()
    for subscription in await live_subscriptions(ctx.session, ctx.client.id):
        label = await _summary(ctx, subscription, None, now)
        keyboard.action(label, Card(subscription_id=subscription.id).pack())
    keyboard.action(ctx.render("btn.back"), BACK_TO_MENU)
    return Screen.plain(ctx.render("subscriptions.list.header"), keyboard)


async def subscriptions_list(query: CallbackQuery, ctx: BotContext) -> None:
    await show(query, ctx, await subscriptions_screen(ctx))


def _row(label: str, value: RichTextUnion) -> list[RichBlockTableCell]:
    return [
        RichBlockTableCell(align="left", valign="top", text=label),
        RichBlockTableCell(align="left", valign="top", text=value),
    ]


def _gigabytes(ctx: BotContext, size: int) -> str:
    language = ctx.texts.language_for(ctx.client.language_code)
    value = format_decimal(size / GIGABYTE, format="#,##0.#", locale=language)
    return ctx.render("unit.gigabytes", value=value)


def _traffic(ctx: BotContext, subscription: Subscription) -> str:
    """«12,4 ГБ из 50 ГБ» или «без ограничения» — по последним данным панели (0060)."""
    limit = subscription.traffic_limit_bytes
    if not limit:
        return ctx.render("card.traffic.unlimited")
    used = _gigabytes(ctx, subscription.traffic_used_bytes or 0)
    return ctx.render("card.traffic.value", used=used, limit=_gigabytes(ctx, limit))


async def subscription_card(ctx: BotContext, subscription: Subscription) -> Screen:
    """З3 — rich: таблица с тарифом, статусом, сроком, устройствами и трафиком, пометки,
    ссылка; кнопки действий — внутри сообщения (0044, 0060; 3.24, 3.36, 4.7, 4.12)."""
    now = _now()
    state = _state(subscription, now)
    tariff = (
        await ctx.session.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    )
    if tariff is not None:
        tariff_name = tariff.name
    else:
        # Триал — тоже условия подписки, а не «тариф не выбран» (0060)
        tariff_name = ctx.render(
            "card.tariff.trial" if subscription.is_trial else "card.tariff.none"
        )
    rows = [
        _row(ctx.render("card.field.tariff"), tariff_name),
        _row(ctx.render("card.field.status"), ctx.render(f"card.status.{state}")),
    ]
    if subscription.expires_at is not None:
        end = rich_date(await date_value(ctx, subscription.expires_at))
        rows.append(_row(ctx.render("card.field.end_date"), end))
        left = _days_left(ctx, subscription, now)
        if left:
            remaining = rich_relative(subscription.expires_at, left)
            rows.append(_row(ctx.render("card.field.remaining"), remaining))
    # «Устройства: занято X из Y» — если в панели включён лимит устройств (З3, 6.1)
    devices = await card_devices(ctx, subscription)
    if devices is not None and devices.limit is not None:
        value = ctx.render("card.devices_value", used=str(devices.used), limit=str(devices.limit))
        rows.append(_row(ctx.render("card.field.devices"), value))
    rows.append(_row(ctx.render("card.field.traffic"), _traffic(ctx, subscription)))
    blocks: list[InputRichBlockUnion] = [
        InputRichBlockSectionHeading(
            text=ctx.render("card.title", subscription_name=subscription.name), size=3
        ),
        InputRichBlockTable(cells=rows, is_bordered=True),
    ]
    notes: list[str] = []
    if tariff is not None and tariff.state == TariffState.ARCHIVED and state != "disabled":
        notes.append("card.note.archived")
    if state == "expired":
        notes.append("card.note.expired")
    if state == "disabled":
        notes.append("card.note.disabled")
    blocks.extend(InputRichBlockParagraph(text=ctx.render(note)) for note in notes)
    if not await panel_available(ctx.session):
        blocks.append(InputRichBlockParagraph(text=ctx.render("menu.stale_data")))
    blocks.append(
        InputRichBlockDetails(
            summary=ctx.render("card.link"),
            blocks=[InputRichBlockParagraph(text=subscription.subscription_url)],
        )
    )

    actions: list[RichMessageButton] = []
    subscriptions = await live_subscriptions(ctx.session, ctx.client.id)
    if disabled_in_panel(subscription):
        # Отключена в панели: продления нет, клиент обращается в поддержку (4.7, 3.36)
        support = await support_url(ctx)
        if support is not None:
            actions.append(rich_link(ctx.render("btn.support"), support))
    elif subscription.is_trial:
        if trial_for_purchase(subscriptions) is subscription:
            actions.append(rich_action(ctx.render("btn.buy"), BUY, style=SUCCESS))
    elif can_renew(subscription):
        renew = Renew(subscription_id=subscription.id).pack()
        actions.append(rich_action(ctx.render("btn.renew"), renew, style=SUCCESS))
        if await change_tariffs(ctx.session, subscription, now):
            change = ChangeTariff(subscription_id=subscription.id).pack()
            actions.append(rich_action(ctx.render("btn.change_tariff"), change))
    if devices is not None:
        devices_screen = DevicesScreen(subscription_id=subscription.id).pack()
        actions.append(rich_action(ctx.render("btn.devices"), devices_screen))
    guide = GuideHere(subscription_id=subscription.id, back=GuideBack.CARD).pack()
    actions.append(rich_action(ctx.render("btn.how_to_connect"), guide))
    actions.append(rich_copy(ctx.render("btn.copy_link"), subscription.subscription_url))
    blocks.extend(button_rows(actions, _CARD_ROW))

    back = SUBSCRIPTIONS if len(subscriptions) > 1 else BACK_TO_MENU
    keyboard = Keyboard().action(ctx.render("btn.back"), back)
    return Screen(keyboard=keyboard, rich=InputRichMessage(blocks=blocks))


async def open_card(query: CallbackQuery, callback_data: Card, ctx: BotContext) -> None:
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await subscription_card(ctx, subscription))


# --- З4. Выбор тарифа ---


async def tariff_choice(
    ctx: BotContext,
    subscription: Subscription | None,
    header: str | None,
    tariffs: list[Tariff] | None = None,
) -> Screen:
    """З4 — rich: тарифы в продаже по порядку (2.4, 3.1); шапка — для продления (3.21, 3.35);
    кнопки тарифов — внутри сообщения (0044, 0060). `tariffs` — свой список, например
    для смены тарифа (3.41)."""
    if tariffs is None:
        tariffs = await tariffs_on_sale(ctx.session)
    head = [
        RichBlockTableCell(align="left", valign="top", text=ctx.render(key), is_header=True)
        for key in (
            "tariffs.col.tariff",
            "tariffs.col.period",
            "tariffs.col.devices",
            "tariffs.col.price",
        )
    ]
    rows = [head]
    for tariff in tariffs:
        rows.append(
            [
                RichBlockTableCell(align="left", valign="top", text=tariff.name),
                RichBlockTableCell(
                    align="left", valign="top", text=_days(ctx, tariff.duration_days or 0)
                ),
                RichBlockTableCell(align="center", valign="top", text=str(tariff.device_limit)),
                RichBlockTableCell(
                    align="right", valign="top", text=await _money(ctx, tariff.price)
                ),
            ]
        )
    blocks: list[InputRichBlockUnion] = [
        InputRichBlockSectionHeading(text=ctx.render("tariffs.title"), size=3)
    ]
    if header is not None:
        blocks.append(InputRichBlockParagraph(text=ctx.render(header)))
    blocks.append(InputRichBlockTable(cells=rows, is_bordered=True))
    blocks.extend(
        InputRichBlockParagraph(text=[RichTextBold(text=tariff.name), " — ", tariff.description])
        for tariff in tariffs
        if tariff.description.strip()
    )
    subscription_id = subscription.id if subscription is not None else 0
    picks = [
        rich_action(
            ctx.render(
                "tariffs.button", tariff_name=tariff.name, price=await _money(ctx, tariff.price)
            ),
            Pick(tariff_id=tariff.id, subscription_id=subscription_id).pack(),
        )
        for tariff in tariffs
    ]
    blocks.extend(button_rows(picks, _TARIFF_ROW))
    if subscription is None and await trial_available(ctx.session, ctx.client):
        # «Попробовать бесплатно» — под тарифами (5.2, 0035)
        trial = rich_action(ctx.render("btn.try_free"), TRY_FREE, style=SUCCESS)
        blocks.extend(button_rows([trial], 1))
    back = (
        Card(subscription_id=subscription.id).pack() if subscription is not None else BACK_TO_MENU
    )
    keyboard = Keyboard().action(ctx.render("btn.back"), back)
    return Screen(keyboard=keyboard, rich=InputRichMessage(blocks=blocks))


async def buy(query: CallbackQuery, ctx: BotContext) -> None:
    """«Купить подписку» — выбор тарифа; при единственной триальной подписке покупка
    применится к ней (3.16, 3.36)."""
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    if not can_purchase(await live_subscriptions(ctx.session, ctx.client.id)):
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await tariff_choice(ctx, None, None))


async def renew(
    query: CallbackQuery, callback_data: Renew, ctx: BotContext, providers: Providers
) -> None:
    """«Продлить»: тариф подписки — сразу подтверждение (3.20, 3.23); тариф в архиве у
    истёкшей подписки или подписка без тарифа — выбор тарифа (3.21, 3.35)."""
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None or not can_renew(subscription):
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await renewal_screen(ctx, subscription, providers))


async def renewal_screen(
    ctx: BotContext, subscription: Subscription, providers: Providers
) -> Screen:
    """Продление подписки: тем же тарифом — сразу подтверждение (3.20, 3.23), иначе —
    выбор тарифа (3.21, 3.35)."""
    tariff = await renewal_tariff(ctx.session, subscription, _now())
    if tariff is not None:
        return await confirmation(ctx, tariff, subscription, providers)
    header = (
        "tariffs.header.no_tariff"
        if subscription.tariff_id is None
        else "tariffs.header.archived_expired"
    )
    return await tariff_choice(ctx, subscription, header)


async def broadcast_action(query: CallbackQuery, ctx: BotContext, providers: Providers) -> None:
    """«Купить» и «Продлить» в рассылке (11.10) ведут в одно место: «Купить» у клиента с
    платной подпиской — как «Продлить», «Продлить» без подписок или с единственной
    триальной — к выбору тарифа (решение 0065, 3.36). Закрытый магазин — «Магазин
    временно закрыт», ещё не открытый — «Магазин скоро откроется» (11.12, 1.13).
    Экран — новым сообщением: рассылка не перезаписывается."""
    await query.answer()
    await (await _broadcast_target(ctx, query.from_user.id, providers)).send(
        ctx.bot, query.from_user.id
    )


async def _broadcast_target(ctx: BotContext, telegram_id: int, providers: Providers) -> Screen:
    if not await sales_allowed(ctx, telegram_id):
        return await paused_screen(ctx)
    subscriptions = await live_subscriptions(ctx.session, ctx.client.id)
    if can_purchase(subscriptions):
        return await tariff_choice(ctx, None, None)
    if not any(can_renew(s) for s in subscriptions):
        # Подписки отключены в панели: продлить нельзя, в меню — «обратитесь в поддержку»
        return await main_menu(ctx)
    if len(subscriptions) > 1:
        # Несколько подписок — выбор подписки, продление — с её экрана (3.24, 3.40)
        return await subscriptions_screen(ctx)
    return await renewal_screen(ctx, subscriptions[0], providers)


async def change_tariff(query: CallbackQuery, callback_data: ChangeTariff, ctx: BotContext) -> None:
    """«Сменить тариф» у истёкшей подписки: тарифы в продаже, кроме текущего; выбранный
    применяется к этой же подписке (3.41, решение 0068)."""
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    tariffs = (
        await change_tariffs(ctx.session, subscription, _now()) if subscription is not None else []
    )
    if subscription is None or not tariffs:
        # Кнопка из старого сообщения: подписку уже продлили или тарифов для смены нет
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await tariff_choice(ctx, subscription, None, tariffs))


# --- Триал ---


async def try_free(query: CallbackQuery, ctx: BotContext) -> None:
    """«Попробовать бесплатно» (5.4–5.6): создание триала уходит в очередь; ссылку и
    срок клиент получит событием «Триал активирован» (С3), а если панель не ответила
    сразу — «Создаём пробную подписку» (С18). Повторное нажатие второй триал не создаёт
    (5.5). Пока магазин временно закрыт — «Магазин временно закрыт» (1.22)."""
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    await request_trial(ctx.session, ctx.client)
    await show(query, ctx, await main_menu(ctx))


# --- З5. Подтверждение ---


async def confirmation(
    ctx: BotContext,
    tariff: Tariff,
    subscription: Subscription | None,
    providers: Providers,
    *,
    promo: PromoCode | None = None,
    note: str | None = None,
    just_applied: bool = False,
) -> Screen:
    """З5: тариф, срок, цена, скидка по промокоду, итог; новая дата окончания, если
    оплата добавит дни (3.3, 8.4). Итог ноль — «Получить» вместо «Оплатить» (8.8).
    `note` — текст над экраном, например почему промокод не подошёл; `just_applied` —
    над экраном «Промокод применён» (З8)."""
    trial = trial_for_purchase(await live_subscriptions(ctx.session, ctx.client.id))
    extended = subscription if subscription is not None else trial
    offer = quote(
        tariff,
        extended,
        _now(),
        discount_percent=promo.discount_percent if promo is not None else None,
        min_amount=await providers.min_amount(ctx.session),
    )
    price = await _money(ctx, offer.price)
    text = ctx.render(
        "confirm.body", tariff_name=tariff.name, period=_days(ctx, offer.days), price=price
    )
    if promo is not None and just_applied:
        discount = await _money(ctx, offer.discount)
        note = ctx.render("promo.discount_applied", promo_code=promo.code, discount=discount)
    if note is not None:
        text = f"{note}\n\n{text}"
    if promo is not None:
        discount = await _money(ctx, offer.discount)
        text = f"{text}\n{ctx.render('confirm.discount', promo_code=promo.code, discount=discount)}"
    text = f"{text}\n\n{ctx.render('confirm.total', total=await _money(ctx, offer.total))}"
    dates = Dates()
    if offer.new_end is not None:
        mark = dates.mark(await date_value(ctx, offer.new_end))
        text = f"{text}\n{ctx.render('confirm.new_end_date', new_end_date=mark)}"
    subscription_id = subscription.id if subscription is not None else 0
    pay = Pay(
        tariff_id=tariff.id,
        subscription_id=subscription_id,
        price=int(offer.total * _KOPECKS),
        token=secrets.token_hex(8),
        promo=promo.id if promo is not None else 0,
    )
    back = Card(subscription_id=subscription.id).pack() if subscription is not None else BUY
    ask = PromoAsk(tariff_id=tariff.id, subscription_id=subscription_id)
    keyboard = (
        Keyboard()
        .action(
            ctx.render("btn.pay" if offer.total > 0 else "btn.get_free"), pay.pack(), style=SUCCESS
        )
        .action(ctx.render("btn.promo"), ask.pack())
        .action(ctx.render("btn.back"), back)
    )
    return Screen.plain(text, keyboard, dates)


async def usable_discount(
    ctx: BotContext, promo_id: int, tariff: Tariff, *, lock: bool = False
) -> tuple[PromoCode | None, str | None]:
    """Промокод на скидку с экрана подтверждения, если он всё ещё подходит. Иначе —
    без него и с причиной (8.12): клиент продолжает без промокода.

    `lock` — перед созданием платежа: клиент и промокод блокируются, чтобы два
    одновременных нажатия не прошли проверку оба (одна активация на клиента, лимит)."""
    if lock:
        await ctx.session.refresh(ctx.client, with_for_update=True)
    promo = await ctx.session.get(PromoCode, promo_id, with_for_update=lock)
    if (
        promo is None
        # Номер промокода в кнопке клиент может подделать: только код, который он ввёл
        or not await is_accepted(ctx.session, ctx.client.id, promo_id)
        or not await still_usable(ctx.session, promo, _now())
    ):
        return None, ctx.render("promo.error.invalid")
    try:
        await check_discount(ctx.session, ctx.client, promo, tariff)
    except PromoRejectedError as error:
        return None, ctx.render(error.reason.text_key)
    return promo, None


async def operation_choice(
    ctx: BotContext, tariff_id: int, subscription_id: int
) -> tuple[PaymentPurpose, Tariff, Subscription | None] | None:
    """Покупка или продление выбранным тариф, если это сейчас можно оформить."""
    tariff = await ctx.session.get(Tariff, tariff_id)
    subscription = None
    if subscription_id:
        subscription = await own_subscription(ctx, subscription_id)
        if subscription is None:
            return None
    purpose = PaymentPurpose.RENEWAL if subscription is not None else PaymentPurpose.PURCHASE
    if tariff is None:
        return None
    try:
        await check_choice(
            ctx.session,
            client_id=ctx.client.id,
            purpose=purpose,
            tariff=tariff,
            subscription=subscription,
            now=_now(),
        )
    except CatalogError:
        return None
    return purpose, tariff, subscription


async def pick(
    query: CallbackQuery, callback_data: Pick, ctx: BotContext, providers: Providers
) -> None:
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    choice = await operation_choice(ctx, callback_data.tariff_id, callback_data.subscription_id)
    if choice is None:
        await show(query, ctx, await main_menu(ctx))
        return
    _purpose, tariff, subscription = choice
    await show(query, ctx, await confirmation(ctx, tariff, subscription, providers))


# --- З6. Оплата ---


async def invoice_screen(ctx: BotContext, payment: Payment) -> Screen:
    """З6: сумма, срок действия счёта, «Перейти к оплате», «Отменить» (3.4, 3.38)."""
    dates = Dates()
    expires = dates.mark(await date_value(ctx, payment.expires_at)) if payment.expires_at else ""
    text = ctx.render(
        "payment.invoice", total=await _money(ctx, payment.amount), invoice_expires_at=expires
    )
    keyboard = Keyboard()
    if payment.payment_url:
        keyboard.link(ctx.render("btn.go_to_payment"), payment.payment_url, style=SUCCESS)
    keyboard.action(ctx.render("btn.cancel"), CancelPay(payment_id=payment.id).pack())
    return Screen.plain(text, keyboard, dates)


def _unavailable(ctx: BotContext) -> Screen:
    """«Оплата временно недоступна» вместо экрана оплаты (3.30)."""
    keyboard = Keyboard().action(ctx.render("btn.main_menu"), BACK_TO_MENU)
    return Screen.plain(ctx.render("payment.unavailable"), keyboard)


async def pay(
    query: CallbackQuery, callback_data: Pay, ctx: BotContext, providers: Providers
) -> None:
    """«Оплатить»: счёт на сумму с экрана подтверждения (3.4); повторное нажатие на
    том же экране — тот же счёт (3.5)."""
    if not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    choice = await operation_choice(ctx, callback_data.tariff_id, callback_data.subscription_id)
    if choice is None:
        await show(query, ctx, await main_menu(ctx))
        return
    purpose, tariff, subscription = choice
    promo: PromoCode | None = None
    if callback_data.promo:
        # Промокод перестал действовать до создания платежа — экран заново, без него
        promo, problem = await usable_discount(ctx, callback_data.promo, tariff, lock=True)
        if promo is None:
            screen = await confirmation(ctx, tariff, subscription, providers, note=problem)
            await show(query, ctx, screen)
            return
    bot = await ctx.bot.me()
    checkout = Checkout(
        client=ctx.client,
        purpose=purpose,
        tariff=tariff,
        subscription=subscription,
        shown_total=Decimal(callback_data.price) / _KOPECKS,
        token=callback_data.token,
        description=ctx.render(
            "payment.description",
            brand_name=await ctx.brand_name(),
            tariff_name=tariff.name,
            period=_days(ctx, tariff.duration_days or 0),
        ),
        return_url=f"https://t.me/{bot.username}",
        promo=CheckoutPromo(promo.id, promo.discount_percent)
        if promo is not None and promo.discount_percent is not None
        else None,
    )
    try:
        payment = await open_invoice(ctx.session, providers, checkout, now=_now())
    except PriceChangedError:
        # Итог изменился, пока клиент смотрел экран: показываем подтверждение заново
        await show(
            query, ctx, await confirmation(ctx, tariff, subscription, providers, promo=promo)
        )
        return
    except PaymentUnavailableError:
        await show(query, ctx, _unavailable(ctx))
        return
    except CheckoutError:
        await show(query, ctx, await main_menu(ctx))
        return
    # Платёж сохранён до того, как клиент увидит ссылку на оплату: иначе сбой при показе
    # экрана откатил бы платёж, а оплата по ссылке стала бы «неизвестной» (4.23)
    await ctx.session.commit()
    if payment.state != PaymentState.PENDING:
        await show(query, ctx, await main_menu(ctx))
        return
    await show(query, ctx, await invoice_screen(ctx, payment))


async def cancel_payment(
    query: CallbackQuery, callback_data: CancelPay, ctx: BotContext, providers: Providers
) -> None:
    """«Отменить» на экране оплаты (3.38) — снова главное меню."""
    await cancel_by_client(
        ctx.session, providers, callback_data.payment_id, client_id=ctx.client.id
    )
    await show(query, ctx, await main_menu(ctx))


async def retry_payment(
    query: CallbackQuery, callback_data: RetryPay, ctx: BotContext, providers: Providers
) -> None:
    """«Попробовать снова» на «Платёж не прошёл» (3.12): подтверждение той же операции
    новым сообщением — событие не перезаписывается."""
    await query.answer()
    payment = await ctx.session.get(Payment, callback_data.payment_id)
    screen: Screen
    if not await sales_allowed(ctx, query.from_user.id):
        screen = await paused_screen(ctx)
    elif payment is None or payment.client_id != ctx.client.id or payment.tariff_id is None:
        screen = await main_menu(ctx)
    else:
        choice = await operation_choice(ctx, payment.tariff_id, payment.subscription_id or 0)
        if choice is not None:
            # Тот же промокод, если он всё ещё подходит (8.12)
            promo = None
            if payment.promo_code_id is not None:
                # Промокод из своего же платежа клиент уже вводил
                await accept(ctx.session, ctx.client.id, payment.promo_code_id)
                promo, _problem = await usable_discount(ctx, payment.promo_code_id, choice[1])
            screen = await confirmation(ctx, choice[1], choice[2], providers, promo=promo)
        elif payment.subscription_id is None:
            screen = await tariff_choice(ctx, None, None)
        else:
            screen = await main_menu(ctx)
    await screen.send(ctx.bot, query.from_user.id)


def build_router() -> Router:
    router = Router(name="shop")
    router.message.register(start, CommandStart())
    router.callback_query.register(menu_again, F.data == MAIN_MENU_CALLBACK)
    router.callback_query.register(back_to_menu, F.data == BACK_TO_MENU)
    router.callback_query.register(subscriptions_list, F.data == SUBSCRIPTIONS)
    router.callback_query.register(open_card, Card.filter())
    router.callback_query.register(buy, F.data == BUY)
    router.callback_query.register(try_free, F.data == TRY_FREE)
    router.callback_query.register(renew, Renew.filter())
    router.callback_query.register(broadcast_action, F.data.in_({BROADCAST_BUY, BROADCAST_RENEW}))
    router.callback_query.register(change_tariff, ChangeTariff.filter())
    router.callback_query.register(pick, Pick.filter())
    router.callback_query.register(pay, Pay.filter())
    router.callback_query.register(cancel_payment, CancelPay.filter())
    router.callback_query.register(retry_payment, RetryPay.filter())
    return router
