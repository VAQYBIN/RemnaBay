"""З8. Ввод промокода (блок 8, сценарий «Промокод»; решения 0065, 0066).

- «Ввести промокод» на экране подтверждения — скидка к выбранному тарифу (8.4);
  в главном меню — дни к подписке (8.10). Бот просит прислать код с force_reply;
  следующее сообщение клиента — код.
- Причины отказа — по 8.12; после нескольких неверных кодов ввод временно
  ограничен (8.13). Клиент всегда может продолжить без промокода: «Назад» ведёт к
  экрану подтверждения без скидки или в главное меню.
- Промокод на дни работает и в закрытом магазине (1.22); скидка — только там, где
  открыта покупка.
"""

from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, ForceReply, Message

from remnabay.bot._common import (
    BACK_TO_MENU,
    PromoAsk,
    paused_screen,
    sales_allowed,
    show,
)
from remnabay.bot._context import BotContext
from remnabay.bot._screens import Dates, Keyboard, Screen, date_value
from remnabay.bot._shop import Pick, confirmation, main_menu, operation_choice
from remnabay.domain.promo import PromoInput
from remnabay.payments import Providers
from remnabay.promo import (
    DaysAdded,
    DaysOutcome,
    PromoRejectedError,
    Rejection,
    activate_days,
    await_code,
    awaiting,
    blocked_minutes,
    days_targets,
    enter_days,
    enter_discount,
    take_awaited,
)


class PromoDays(CallbackData, prefix="promodays"):
    """Подписка, к которой добавить дни по промокоду, если их несколько (8.10)."""

    promo_id: int
    subscription_id: int


def _now() -> datetime:
    return datetime.now(UTC)


def _confirm_back(ask: PromoAsk) -> str:
    """«Назад» к экрану подтверждения того же тарифа — без промокода (8.12)."""
    return Pick(tariff_id=ask.tariff_id, subscription_id=ask.subscription_id).pack()


def _rejected(ctx: BotContext, error: PromoRejectedError, ask: PromoAsk) -> Screen:
    """Почему код не принят (8.12, 8.13): «Ввести промокод» ещё раз и «Назад» —
    продолжить без промокода. Пока ввод ограничен, вторую попытку не предлагаем."""
    if error.reason == Rejection.RATE_LIMITED:
        minutes = ctx.texts.quantity("unit.minutes", ctx.client.language_code, error.minutes)
        text = ctx.render(error.reason.text_key, minutes_text=minutes)
    else:
        text = ctx.render(error.reason.text_key)
    keyboard = Keyboard()
    if error.reason != Rejection.RATE_LIMITED:
        keyboard.action(ctx.render("btn.promo"), ask.pack())
    back = _confirm_back(ask) if ask.tariff_id else BACK_TO_MENU
    keyboard.action(ctx.render("btn.back"), back)
    return Screen.plain(text, keyboard)


async def ask_code(query: CallbackQuery, callback_data: PromoAsk, ctx: BotContext) -> None:
    """«Ввести промокод»: бот просит прислать код сообщением (З8)."""
    now = _now()
    from_confirm = callback_data.tariff_id != 0
    if from_confirm and not await sales_allowed(ctx, query.from_user.id):
        await show(query, ctx, await paused_screen(ctx))
        return
    minutes = await blocked_minutes(ctx.session, ctx.client.id, now)
    if minutes:
        error = PromoRejectedError(Rejection.RATE_LIMITED, minutes)
        await show(query, ctx, _rejected(ctx, error, callback_data))
        return
    await await_code(
        ctx.session,
        ctx.client.id,
        PromoInput.CONFIRM if from_confirm else PromoInput.MENU,
        now,
        tariff_id=callback_data.tariff_id or None,
        subscription_id=callback_data.subscription_id or None,
    )
    await query.answer()
    await ctx.bot.send_message(
        query.from_user.id, ctx.render("promo.prompt"), reply_markup=ForceReply()
    )


async def awaiting_code(message: Message, ctx: BotContext) -> bool:
    """Фильтр: сообщение — код, если бот его ждёт и это не команда."""
    text = message.text
    if text is None or text.startswith("/"):
        return False
    return await awaiting(ctx.session, ctx.client.id, _now())


async def receive_code(message: Message, ctx: BotContext, providers: Providers) -> None:
    """Код прислан: скидка — экран подтверждения с новым итогом (8.4); дни — выбор
    подписки или результат (8.10); иначе — причина (8.12)."""
    now = _now()
    awaited = await take_awaited(ctx.session, ctx.client.id, now)
    text = message.text or ""
    telegram_id = message.chat.id
    if awaited is None:
        screen = await main_menu(ctx)
    elif awaited.source == PromoInput.CONFIRM and awaited.tariff_id is not None:
        ask = PromoAsk(tariff_id=awaited.tariff_id, subscription_id=awaited.subscription_id or 0)
        screen = await _discount(ctx, providers, ask, text, telegram_id)
    else:
        screen = await _days(ctx, text, now)
    await screen.send(ctx.bot, telegram_id)


async def _discount(
    ctx: BotContext, providers: Providers, ask: PromoAsk, text: str, telegram_id: int
) -> Screen:
    if not await sales_allowed(ctx, telegram_id):
        return await paused_screen(ctx)
    choice = await operation_choice(ctx, ask.tariff_id, ask.subscription_id)
    if choice is None:
        return await main_menu(ctx)
    _purpose, tariff, subscription = choice
    try:
        promo = await enter_discount(ctx.session, ctx.client, text, tariff, _now())
    except PromoRejectedError as error:
        return _rejected(ctx, error, ask)
    return await confirmation(ctx, tariff, subscription, providers, promo=promo, just_applied=True)


async def _days(ctx: BotContext, text: str, now: datetime) -> Screen:
    try:
        promo = await enter_days(ctx.session, ctx.client, text, now)
    except PromoRejectedError as error:
        return _rejected(ctx, error, PromoAsk(tariff_id=0, subscription_id=0))
    days = promo.days or 0
    targets = await days_targets(ctx.session, ctx.client.id)
    if len(targets) == 1:
        return await _activate(ctx, promo.id, targets[0].id)
    # Подписок несколько — клиент выбирает, к какой добавить дни (8.10)
    keyboard = Keyboard()
    for subscription in targets:
        choice = PromoDays(promo_id=promo.id, subscription_id=subscription.id)
        keyboard.action(subscription.name, choice.pack())
    keyboard.action(ctx.render("btn.back"), BACK_TO_MENU)
    days_text = ctx.texts.quantity("unit.days", ctx.client.language_code, days)
    return Screen.plain(ctx.render("promo.days_choose_subscription", days_text=days_text), keyboard)


async def _activate(ctx: BotContext, promo_id: int, subscription_id: int) -> Screen:
    try:
        outcome = await activate_days(
            ctx.session, ctx.panel, ctx.client, promo_id, subscription_id, _now()
        )
    except PromoRejectedError as error:
        return _rejected(ctx, error, PromoAsk(tariff_id=0, subscription_id=0))
    return await _days_result(ctx, outcome)


async def _days_result(ctx: BotContext, outcome: DaysOutcome) -> Screen:
    """Дни добавлены — новая дата окончания; ушли в очередь — «в ближайшие минуты» (8.15)."""
    subscription = outcome.subscription
    keyboard = Keyboard().action(ctx.render("btn.main_menu"), BACK_TO_MENU)
    if outcome.result == DaysAdded.QUEUED or subscription.expires_at is None:
        text = ctx.render("promo.days_queued", subscription_name=subscription.name)
        return Screen.plain(text, keyboard)
    dates = Dates()
    end_date = dates.mark(await date_value(ctx, subscription.expires_at))
    text = ctx.render(
        "promo.days_applied",
        subscription_name=subscription.name,
        days_text=ctx.texts.quantity("unit.days", ctx.client.language_code, outcome.days),
        end_date=end_date,
    )
    return Screen.plain(text, keyboard, dates)


async def choose_subscription(
    query: CallbackQuery, callback_data: PromoDays, ctx: BotContext
) -> None:
    """Клиент выбрал подписку для дней по промокоду (8.10)."""
    await show(
        query, ctx, await _activate(ctx, callback_data.promo_id, callback_data.subscription_id)
    )


def build_router() -> Router:
    router = Router(name="promo")
    router.callback_query.register(ask_code, PromoAsk.filter())
    router.callback_query.register(choose_subscription, PromoDays.filter())
    router.message.register(receive_code, awaiting_code)
    return router
