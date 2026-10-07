"""Клиент в боте: главное меню, подписки, выбор тарифа, подтверждение, оплата (блок 3).

Экраны З1–З6 (03-screens). Покупка и продление закрыты, пока магазин временно
закрыт (1.22): клиент видит «Магазин временно закрыт», а свои подписки — как
обычно. Для команды и тестировщиков бот работает как обычно (1.21).
"""

import secrets
from datetime import UTC, datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichBlockUnion,
    InputRichMessage,
    Message,
    RichBlockTableCell,
    RichTextBold,
    RichTextUnion,
)
from babel.numbers import format_currency

from remnabay.bot._context import BotContext
from remnabay.bot._screens import SUCCESS, Dates, Keyboard, Screen, date_value, rich_date
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import MAIN_MENU_CALLBACK
from remnabay.panel_sync import panel_available
from remnabay.payments import (
    CatalogError,
    Checkout,
    CheckoutError,
    PaymentUnavailableError,
    PriceChangedError,
    Providers,
    can_purchase,
    can_renew,
    cancel_by_client,
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
from remnabay.shop import SHOP_CURRENCY, SHOP_SUPPORT_CONTACT, is_tester, sales_open
from remnabay.shop_settings import get_setting

# Ключ данных обработчика: провайдеры с ключами оператора (3.27)
PROVIDERS_KEY = "providers"
# «Купить подписку», «Мои подписки», «Назад» к меню (правка того же сообщения)
BUY = "buy"
SUBSCRIPTIONS = "subs"
BACK_TO_MENU = "menu:back"
_KOPECKS = 100


class Card(CallbackData, prefix="sub"):
    subscription_id: int


class Renew(CallbackData, prefix="renew"):
    subscription_id: int


class Pick(CallbackData, prefix="pick"):
    """Выбор тарифа: покупка (`subscription_id` = 0) или продление подписки."""

    tariff_id: int
    subscription_id: int


class Pay(CallbackData, prefix="pay"):
    """«Оплатить» на экране подтверждения: цена, которую видел клиент, и токен экрана (3.4, 3.5)."""

    tariff_id: int
    subscription_id: int
    price: int
    token: str


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


async def _support_url(ctx: BotContext) -> str | None:
    """Контакт поддержки как ссылка: «@имя», «t.me/имя» или адрес сайта."""
    contact = (await get_setting(ctx.session, SHOP_SUPPORT_CONTACT)).strip()
    if contact.startswith("@") and len(contact) > 1:
        return f"https://t.me/{contact[1:]}"
    if contact.startswith(("https://", "http://")):
        return contact
    if contact.startswith("t.me/"):
        return f"https://{contact}"
    return None


async def _sales_allowed(ctx: BotContext, telegram_id: int) -> bool:
    """Покупка и продление: в открытом магазине; для команды и тестировщиков — всегда (1.22)."""
    if await sales_open(ctx.session) or ctx.member is not None:
        return True
    return await is_tester(ctx.session, telegram_id)


async def _paused(ctx: BotContext) -> Screen:
    """З0а: «Магазин временно закрыт» вместо покупки и продления (1.22)."""
    text = ctx.render("shop.paused", brand_name=await ctx.brand_name())
    keyboard = Keyboard().action(ctx.render("btn.main_menu"), BACK_TO_MENU)
    return Screen.plain(text, keyboard)


async def _show(query: CallbackQuery, ctx: BotContext, screen: Screen) -> None:
    """Экран вместо текущего сообщения меню."""
    await query.answer()
    message = query.message
    if message is None or isinstance(message, InaccessibleMessage):
        await screen.send(ctx.bot, query.from_user.id)
        return
    await screen.edit(ctx.bot, query.from_user.id, message.message_id)


def _state(subscription: Subscription, now: datetime) -> str:
    """Состояние подписки для клиента — по данным панели (01-domain)."""
    if subscription.panel_status == PanelUserStatus.DISABLED:
        return "disabled"
    if subscription.panel_status == PanelUserStatus.LIMITED:
        return "limited"
    if subscription.panel_status == PanelUserStatus.EXPIRED or not is_active(subscription, now):
        return "expired"
    return "active"


async def _summary(
    ctx: BotContext, subscription: Subscription, dates: Dates | None, now: datetime
) -> str:
    """Строка сводки: название, статус, дата окончания (З1, З2)."""
    state = _state(subscription, now)
    end_date = ""
    if state in ("active", "expired") and subscription.expires_at is not None:
        value = await date_value(ctx, subscription.expires_at)
        end_date = dates.mark(value) if dates is not None else value.fallback
    return ctx.render(f"summary.{state}", subscription_name=subscription.name, end_date=end_date)


async def _own_subscription(ctx: BotContext, subscription_id: int) -> Subscription | None:
    for subscription in await live_subscriptions(ctx.session, ctx.client.id):
        if subscription.id == subscription_id:
            return subscription
    return None


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
        text = ctx.render(
            "menu.header.subscriptions", brand_name=brand, subscriptions_summary="\n".join(lines)
        )
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
    if can_purchase(subscriptions):
        # Без подписок или с единственной триальной — «Купить подписку» (3.36)
        keyboard.action(ctx.render("btn.buy"), BUY, style=SUCCESS)
    support = await _support_url(ctx)
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
    await _show(query, ctx, await main_menu(ctx))


# --- З2, З3. Подписки ---


async def subscriptions_list(query: CallbackQuery, ctx: BotContext) -> None:
    """З2: каждая подписка — отдельной кнопкой с названием и статусом (3.24)."""
    now = _now()
    keyboard = Keyboard()
    for subscription in await live_subscriptions(ctx.session, ctx.client.id):
        label = await _summary(ctx, subscription, None, now)
        keyboard.action(label, Card(subscription_id=subscription.id).pack())
    keyboard.action(ctx.render("btn.back"), BACK_TO_MENU)
    await _show(query, ctx, Screen.plain(ctx.render("subscriptions.list.header"), keyboard))


def _row(label: str, value: RichTextUnion) -> list[RichBlockTableCell]:
    return [
        RichBlockTableCell(align="left", valign="top", text=label, is_header=True),
        RichBlockTableCell(align="left", valign="top", text=value),
    ]


async def subscription_card(ctx: BotContext, subscription: Subscription) -> Screen:
    """З3 — rich: название, тариф, статус, дата окончания, пометки (3.24, 3.36, 4.7, 4.12)."""
    now = _now()
    state = _state(subscription, now)
    tariff = (
        await ctx.session.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    )
    rows = [
        _row(
            ctx.render("card.field.tariff"),
            tariff.name if tariff is not None else ctx.render("card.tariff.none"),
        ),
        _row(ctx.render("card.field.status"), ctx.render(f"card.status.{state}")),
    ]
    if subscription.expires_at is not None:
        end = rich_date(await date_value(ctx, subscription.expires_at))
        rows.append(_row(ctx.render("card.field.end_date"), end))
    blocks: list[InputRichBlockUnion] = [
        InputRichBlockSectionHeading(text=subscription.name, size=3),
        InputRichBlockTable(cells=rows),
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

    keyboard = Keyboard()
    subscriptions = await live_subscriptions(ctx.session, ctx.client.id)
    if disabled_in_panel(subscription):
        # Отключена в панели: продления нет, клиент обращается в поддержку (4.7, 3.36)
        support = await _support_url(ctx)
        if support is not None:
            keyboard.link(ctx.render("btn.support"), support)
    elif subscription.is_trial:
        if trial_for_purchase(subscriptions) is subscription:
            keyboard.action(ctx.render("btn.buy"), BUY, style=SUCCESS)
    elif can_renew(subscription):
        keyboard.action(
            ctx.render("btn.renew"), Renew(subscription_id=subscription.id).pack(), style=SUCCESS
        )
    keyboard.copy(ctx.render("btn.copy_link"), subscription.subscription_url)
    back = SUBSCRIPTIONS if len(subscriptions) > 1 else BACK_TO_MENU
    keyboard.action(ctx.render("btn.back"), back)
    return Screen(keyboard=keyboard, rich=InputRichMessage(blocks=blocks))


async def open_card(query: CallbackQuery, callback_data: Card, ctx: BotContext) -> None:
    subscription = await _own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await _show(query, ctx, await main_menu(ctx))
        return
    await _show(query, ctx, await subscription_card(ctx, subscription))


# --- З4. Выбор тарифа ---


async def tariff_choice(
    ctx: BotContext, subscription: Subscription | None, header: str | None
) -> Screen:
    """З4 — rich: тарифы в продаже по порядку (2.4, 3.1); шапка — для продления (3.21, 3.35)."""
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
                RichBlockTableCell(align="left", valign="top", text=RichTextBold(text=tariff.name)),
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
    blocks.append(InputRichBlockTable(cells=rows))
    blocks.extend(
        InputRichBlockParagraph(text=[RichTextBold(text=tariff.name), " — ", tariff.description])
        for tariff in tariffs
        if tariff.description.strip()
    )
    keyboard = Keyboard()
    subscription_id = subscription.id if subscription is not None else 0
    for tariff in tariffs:
        label = ctx.render(
            "tariffs.button", tariff_name=tariff.name, price=await _money(ctx, tariff.price)
        )
        keyboard.action(label, Pick(tariff_id=tariff.id, subscription_id=subscription_id).pack())
    back = (
        Card(subscription_id=subscription.id).pack() if subscription is not None else BACK_TO_MENU
    )
    keyboard.action(ctx.render("btn.back"), back)
    return Screen(keyboard=keyboard, rich=InputRichMessage(blocks=blocks))


async def buy(query: CallbackQuery, ctx: BotContext) -> None:
    """«Купить подписку» — выбор тарифа; при единственной триальной подписке покупка
    применится к ней (3.16, 3.36)."""
    if not await _sales_allowed(ctx, query.from_user.id):
        await _show(query, ctx, await _paused(ctx))
        return
    if not can_purchase(await live_subscriptions(ctx.session, ctx.client.id)):
        await _show(query, ctx, await main_menu(ctx))
        return
    await _show(query, ctx, await tariff_choice(ctx, None, None))


async def renew(query: CallbackQuery, callback_data: Renew, ctx: BotContext) -> None:
    """«Продлить»: тариф подписки — сразу подтверждение (3.20, 3.23); тариф в архиве у
    истёкшей подписки или подписка без тарифа — выбор тарифа (3.21, 3.35)."""
    if not await _sales_allowed(ctx, query.from_user.id):
        await _show(query, ctx, await _paused(ctx))
        return
    subscription = await _own_subscription(ctx, callback_data.subscription_id)
    if subscription is None or not can_renew(subscription):
        await _show(query, ctx, await main_menu(ctx))
        return
    tariff = await renewal_tariff(ctx.session, subscription, _now())
    if tariff is not None:
        await _show(query, ctx, await confirmation(ctx, tariff, subscription))
        return
    header = (
        "tariffs.header.no_tariff"
        if subscription.tariff_id is None
        else "tariffs.header.archived_expired"
    )
    await _show(query, ctx, await tariff_choice(ctx, subscription, header))


# --- З5. Подтверждение ---


async def confirmation(
    ctx: BotContext, tariff: Tariff, subscription: Subscription | None
) -> Screen:
    """З5: тариф, срок, цена, итог; новая дата окончания, если оплата добавит дни (3.3)."""
    trial = trial_for_purchase(await live_subscriptions(ctx.session, ctx.client.id))
    extended = subscription if subscription is not None else trial
    offer = quote(tariff, extended, _now())
    price = await _money(ctx, offer.price)
    text = ctx.render(
        "confirm.body", tariff_name=tariff.name, period=_days(ctx, offer.days), price=price
    )
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
    )
    back = Card(subscription_id=subscription.id).pack() if subscription is not None else BUY
    keyboard = (
        Keyboard()
        .action(ctx.render("btn.pay"), pay.pack(), style=SUCCESS)
        .action(ctx.render("btn.back"), back)
    )
    return Screen.plain(text, keyboard, dates)


async def _choice(
    ctx: BotContext, tariff_id: int, subscription_id: int
) -> tuple[PaymentPurpose, Tariff, Subscription | None] | None:
    """Покупка или продление выбранным тариф, если это сейчас можно оформить."""
    tariff = await ctx.session.get(Tariff, tariff_id)
    subscription = None
    if subscription_id:
        subscription = await _own_subscription(ctx, subscription_id)
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


async def pick(query: CallbackQuery, callback_data: Pick, ctx: BotContext) -> None:
    if not await _sales_allowed(ctx, query.from_user.id):
        await _show(query, ctx, await _paused(ctx))
        return
    choice = await _choice(ctx, callback_data.tariff_id, callback_data.subscription_id)
    if choice is None:
        await _show(query, ctx, await main_menu(ctx))
        return
    _purpose, tariff, subscription = choice
    await _show(query, ctx, await confirmation(ctx, tariff, subscription))


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
    if not await _sales_allowed(ctx, query.from_user.id):
        await _show(query, ctx, await _paused(ctx))
        return
    choice = await _choice(ctx, callback_data.tariff_id, callback_data.subscription_id)
    if choice is None:
        await _show(query, ctx, await main_menu(ctx))
        return
    purpose, tariff, subscription = choice
    bot = await ctx.bot.me()
    checkout = Checkout(
        client=ctx.client,
        purpose=purpose,
        tariff=tariff,
        subscription=subscription,
        shown_price=Decimal(callback_data.price) / _KOPECKS,
        token=callback_data.token,
        description=ctx.render(
            "payment.description",
            brand_name=await ctx.brand_name(),
            tariff_name=tariff.name,
            period=_days(ctx, tariff.duration_days or 0),
        ),
        return_url=f"https://t.me/{bot.username}",
    )
    try:
        payment = await open_invoice(ctx.session, providers, checkout, now=_now())
    except PriceChangedError:
        # Цена изменилась, пока клиент смотрел экран: показываем подтверждение заново
        await _show(query, ctx, await confirmation(ctx, tariff, subscription))
        return
    except PaymentUnavailableError:
        await _show(query, ctx, _unavailable(ctx))
        return
    except CheckoutError:
        await _show(query, ctx, await main_menu(ctx))
        return
    # Платёж сохранён до того, как клиент увидит ссылку на оплату: иначе сбой при показе
    # экрана откатил бы платёж, а оплата по ссылке стала бы «неизвестной» (4.23)
    await ctx.session.commit()
    if payment.state != PaymentState.PENDING:
        await _show(query, ctx, await main_menu(ctx))
        return
    await _show(query, ctx, await invoice_screen(ctx, payment))


async def cancel_payment(
    query: CallbackQuery, callback_data: CancelPay, ctx: BotContext, providers: Providers
) -> None:
    """«Отменить» на экране оплаты (3.38) — снова главное меню."""
    await cancel_by_client(
        ctx.session, providers, callback_data.payment_id, client_id=ctx.client.id
    )
    await _show(query, ctx, await main_menu(ctx))


async def retry_payment(query: CallbackQuery, callback_data: RetryPay, ctx: BotContext) -> None:
    """«Попробовать снова» на «Платёж не прошёл» (3.12): подтверждение той же операции
    новым сообщением — событие не перезаписывается."""
    await query.answer()
    payment = await ctx.session.get(Payment, callback_data.payment_id)
    screen: Screen
    if not await _sales_allowed(ctx, query.from_user.id):
        screen = await _paused(ctx)
    elif payment is None or payment.client_id != ctx.client.id or payment.tariff_id is None:
        screen = await main_menu(ctx)
    else:
        choice = await _choice(ctx, payment.tariff_id, payment.subscription_id or 0)
        if choice is not None:
            screen = await confirmation(ctx, choice[1], choice[2])
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
    router.callback_query.register(renew, Renew.filter())
    router.callback_query.register(pick, Pick.filter())
    router.callback_query.register(pay, Pay.filter())
    router.callback_query.register(cancel_payment, CancelPay.filter())
    router.callback_query.register(retry_payment, RetryPay.filter())
    return router
