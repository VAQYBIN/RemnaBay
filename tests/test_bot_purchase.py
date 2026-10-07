"""Покупка и продление в боте: экраны З1–З6 (блок 3; 1.22, 3.1–3.5, 3.12, 3.15, 3.20–3.24,
3.30, 3.32, 3.35, 3.36, 3.38, 4.7)."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    MessageEntity,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._shop import BUY, Card, Pay, Pick, Renew, RetryPay
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import entries_for
from remnabay.messaging import MAIN_MENU_CALLBACK
from remnabay.payments import ProviderStatus, ShopApplier, apply_payment, payment_subject
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask
from remnabay.shop import SHOP_STATE, SHOP_TESTERS, ShopState
from tests.bot_support import BOT_NAME, BOT_USERNAME, telegram_user
from tests.domain_support import add, make_client, make_subscription, make_tariff, put_setting
from tests.panel_support import fake_runtime
from tests.payment_support import (
    SIGNATURE,
    VALID_SIGNATURE,
    FakeKind,
    FakeProvider,
    connect_fake,
    fake_providers,
)
from tests.web_support import Shop, running_shop

USER = telegram_user(777)
NOW = datetime.now(UTC)

type Screen = SendMessage | SendRichMessage | EditMessageText


@pytest.fixture
def kind() -> FakeKind:
    return FakeKind()


@pytest.fixture
def provider(kind: FakeKind) -> FakeProvider:
    return kind.provider


@pytest.fixture
async def shop(
    valid_env: dict[str, str], db_session: AsyncSession, kind: FakeKind
) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        shop.use_providers(fake_providers(kind))
        await connect_fake(db_session)
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        yield shop


@pytest.fixture
async def tariffs(shop: Shop) -> list[Tariff]:
    month = make_tariff(name="Месяц", duration_days=30, price=Decimal("199.00"), sort_order=1)
    year = make_tariff(name="Год", duration_days=365, price=Decimal("1990.00"), sort_order=0)
    archived = make_tariff(name="Старый", state=TariffState.ARCHIVED, sort_order=2)
    await add(shop.session, month, year, archived)
    return [month, year, archived]


async def _client(shop: Shop) -> Client:
    """Клиент, который уже открывал бот (тот же Telegram ID, что у USER)."""
    client = await shop.session.scalar(
        select(Client).join(TelegramAccount).where(TelegramAccount.telegram_id == USER.id)
    )
    if client is not None:
        return client
    return await make_client(shop.session, telegram_id=USER.id)


async def _subscription(
    shop: Shop,
    tariff: Tariff | None,
    *,
    expires_in: timedelta = timedelta(days=10),
    panel_user_id: int = 1,
    is_trial: bool = False,
    status: PanelUserStatus = PanelUserStatus.ACTIVE,
) -> Subscription:
    client = await _client(shop)
    subscription = make_subscription(client, tariff, panel_user_id=panel_user_id)
    subscription.expires_at = NOW + expires_in
    subscription.is_trial = is_trial
    subscription.panel_status = status
    await add(shop.session, subscription)
    return subscription


def _buttons(screen: Screen) -> list[InlineKeyboardButton]:
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button for row in markup.inline_keyboard for button in row]


def _labels(screen: Screen) -> list[str]:
    return [button.text for button in _buttons(screen)]


def _button(screen: Screen, text: str) -> InlineKeyboardButton:
    [button] = [b for b in _buttons(screen) if b.text == text]
    return button


def _text(screen: Screen) -> str:
    assert not isinstance(screen, SendRichMessage)
    assert screen.text is not None
    return screen.text


def _entities(screen: Screen) -> list[MessageEntity]:
    assert not isinstance(screen, SendRichMessage)
    return screen.entities or []


def _rich(screen: Screen) -> InputRichMessage:
    assert not isinstance(screen, SendMessage)
    assert screen.rich_message is not None
    return screen.rich_message


def _rich_text(screen: Screen) -> str:
    return _rich(screen).model_dump_json()


async def _press(shop: Shop, button: InlineKeyboardButton) -> Screen:
    assert button.callback_data is not None
    await shop.press(USER, button.callback_data)
    return shop.telegram.last()


async def _menu(shop: Shop) -> Screen:
    await shop.send_text(USER, "/start")
    return shop.telegram.last()


async def _payments(shop: Shop) -> list[Payment]:
    return list(await shop.session.scalars(select(Payment).order_by(Payment.id)))


# --- Покупка ---


@pytest.mark.usefixtures("tariffs")
async def test_3_1_3_2_buy_shows_tariffs_on_sale_in_owner_order(shop: Shop) -> None:
    """3.1, 3.2, 2.4: новый клиент видит «Купить подписку»; список — тарифы в продаже в
    порядке владельца, архивного нет."""
    menu = await _menu(shop)
    assert _labels(menu) == ["Купить подписку"]
    assert _button(menu, "Купить подписку").style == "success"

    choice = await _press(shop, _button(menu, "Купить подписку"))

    assert isinstance(choice, EditMessageText)
    assert _labels(choice) == ["Год — 1\xa0990,00\xa0₽", "Месяц — 199,00\xa0₽", "Назад"]
    content = _rich_text(choice)
    assert "Старый" not in content
    assert "30 дней" in content
    assert "365 дней" in content


async def test_3_3_confirmation_shows_tariff_period_price_total(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.3: экран подтверждения — тариф, срок, цена, итог к оплате."""
    month = tariffs[0]
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=month.id, subscription_id=0).pack())
    screen = shop.telegram.last()
    assert "Тариф: Месяц" in _text(screen)
    assert "Срок: 30 дней" in _text(screen)
    assert "Итого: 199,00\xa0₽" in _text(screen)
    assert _labels(screen) == ["Оплатить", "Назад"]
    assert _button(screen, "Оплатить").style == "success"


async def test_3_4_3_32_pay_creates_invoice_and_opens_payment_page(
    shop: Shop, tariffs: list[Tariff], provider: FakeProvider
) -> None:
    """3.4, 3.32: «Оплатить» — счёт на сумму с экрана подтверждения и переход к оплате;
    после оплаты провайдер вернёт клиента в бот."""
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    invoice = await _press(shop, _button(shop.telegram.last(), "Оплатить"))

    [payment] = await _payments(shop)
    assert (payment.state, payment.amount, payment.purpose) == (
        PaymentState.PENDING,
        Decimal("199.00"),
        PaymentPurpose.PURCHASE,
    )
    assert "Счёт на 199,00\xa0₽ создан" in _text(invoice)
    go = _button(invoice, "Перейти к оплате")
    assert (go.url, go.style) == (payment.payment_url, "success")
    [request] = provider.requests
    assert request.return_url == f"https://t.me/{BOT_USERNAME}"
    # Пока название бренда не задано — имя бота в Telegram (0038)
    assert request.description == f"{BOT_NAME}: Месяц, 30 дней"


async def test_3_4_invoice_expiry_is_date_entity(shop: Shop, tariffs: list[Tariff]) -> None:
    """3.4, 0044: срок действия счёта — дата в часовом поясе клиента (date_time)."""
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    invoice = await _press(shop, _button(shop.telegram.last(), "Оплатить"))
    assert isinstance(invoice, EditMessageText)
    [entity] = _entities(invoice)
    assert (entity.type, entity.date_time_format) == ("date_time", "Dt")


async def test_3_5_double_pay_on_one_screen_creates_one_invoice(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.5: двойное «Оплатить» на одном экране подтверждения — один счёт."""
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    pay = _button(shop.telegram.last(), "Оплатить")
    await _press(shop, pay)
    await _press(shop, pay)
    assert len(await _payments(shop)) == 1


async def test_3_4_changed_price_shows_confirmation_again(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.4: цена изменилась, пока клиент смотрел экран, — счёт не создан, клиент видит
    подтверждение с новой ценой."""
    month = tariffs[0]
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=month.id, subscription_id=0).pack())
    pay = _button(shop.telegram.last(), "Оплатить")
    month.price = Decimal("249.00")
    await shop.session.flush()

    screen = await _press(shop, pay)
    assert await _payments(shop) == []
    assert "249,00" in _text(screen)


async def test_3_30_payment_unavailable(
    shop: Shop, tariffs: list[Tariff], provider: FakeProvider
) -> None:
    """3.30: провайдер недоступен — «Оплата временно недоступна»; ничего не создано."""
    provider.unavailable = True
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    screen = await _press(shop, _button(shop.telegram.last(), "Оплатить"))
    assert _text(screen).startswith("Оплата временно недоступна")
    assert await _payments(shop) == []


async def test_3_38_cancel_on_payment_screen(
    shop: Shop, tariffs: list[Tariff], provider: FakeProvider
) -> None:
    """3.38: «Отменить» на экране оплаты — платёж «отменён», счёт отменён у провайдера."""
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    invoice = await _press(shop, _button(shop.telegram.last(), "Оплатить"))
    menu = await _press(shop, _button(invoice, "Отменить"))

    [payment] = await _payments(shop)
    assert payment.state == PaymentState.CANCELLED
    assert provider.cancelled == [payment.provider_payment_id]
    assert "Купить подписку" in _labels(menu)


async def test_3_12_try_again_opens_confirmation_as_new_message(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.12: «Попробовать снова» на «Платёж не прошёл» — подтверждение той же покупки
    новым сообщением (событие не перезаписывается)."""
    client = await _client(shop)
    payment = Payment(
        client_id=client.id,
        purpose=PaymentPurpose.PURCHASE,
        state=PaymentState.DECLINED,
        tariff_id=tariffs[0].id,
        tariff_snapshot={},
        amount=Decimal("199.00"),
        currency="RUB",
    )
    await add(shop.session, payment)
    await shop.press(USER, RetryPay(payment_id=payment.id).pack())

    screen = shop.telegram.last()
    assert isinstance(screen, SendMessage)
    assert "Тариф: Месяц" in _text(screen)
    assert "Оплатить" in _labels(screen)


async def test_main_menu_on_event_opens_new_message(shop: Shop) -> None:
    """03-screens: «Главное меню» на событии присылает меню новым сообщением."""
    await shop.press(USER, MAIN_MENU_CALLBACK)
    assert isinstance(shop.telegram.last(), SendMessage)


# --- Продление ---


async def test_3_23_3_20_single_subscription_renews_with_its_tariff(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.23, 3.20, 3.17: одна подписка — «Моя подписка» сразу ведёт к ней; «Продлить» —
    сразу подтверждение текущим тарифом с новой датой окончания."""
    month = tariffs[0]
    subscription = await _subscription(shop, month)
    menu = await _menu(shop)
    assert _labels(menu) == ["Моя подписка"]
    assert _entities(menu)  # дата окончания в сводке — date_time

    card = await _press(shop, _button(menu, "Моя подписка"))
    assert isinstance(card, EditMessageText)
    assert "Месяц" in _rich_text(card)
    assert _labels(card) == ["Продлить", "Скопировать ссылку", "Назад"]
    assert _button(card, "Скопировать ссылку").copy_text is not None

    confirm = await _press(shop, _button(card, "Продлить"))
    assert "Подписка будет действовать до" in _text(confirm)
    [entity] = _entities(confirm)
    assert subscription.expires_at is not None
    assert entity.unix_time == int((subscription.expires_at + timedelta(days=30)).timestamp())

    await _press(shop, _button(confirm, "Оплатить"))
    [payment] = await _payments(shop)
    assert (payment.purpose, payment.subscription_id) == (PaymentPurpose.RENEWAL, subscription.id)


async def test_3_20_archived_tariff_renews_while_subscription_is_active(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.20, 3.2: тариф в архиве, подписка активна — продление по текущей цене тарифа."""
    archived = tariffs[2]
    subscription = await _subscription(shop, archived)
    await _menu(shop)
    await shop.press(USER, Renew(subscription_id=subscription.id).pack())
    screen = shop.telegram.last()
    assert "Тариф: Старый" in _text(screen)


async def test_3_21_expired_subscription_on_archived_tariff_chooses_new_tariff(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.21: подписка истекла, её тариф в архиве — выбор тарифа в продаже, и он
    применяется к этой же подписке."""
    subscription = await _subscription(shop, tariffs[2], expires_in=-timedelta(days=1))
    await _menu(shop)
    await shop.press(USER, Renew(subscription_id=subscription.id).pack())
    choice = shop.telegram.last()
    assert "Ваш тариф больше не продаётся" in _rich_text(choice)
    assert "Старый" not in " ".join(_labels(choice))

    confirm = await _press(shop, _buttons(choice)[0])
    await _press(shop, _button(confirm, "Оплатить"))
    [payment] = await _payments(shop)
    assert (payment.purpose, payment.subscription_id) == (PaymentPurpose.RENEWAL, subscription.id)
    assert payment.tariff_id == tariffs[1].id


async def test_3_35_subscription_without_tariff_chooses_tariff(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.35: у подписки нет тарифа — «Продлить» ведёт к выбору тарифа из тех, что в продаже."""
    del tariffs
    subscription = await _subscription(shop, None)
    await _menu(shop)
    await shop.press(USER, Renew(subscription_id=subscription.id).pack())
    assert "Выберите тариф для продления" in _rich_text(shop.telegram.last())


async def test_3_24_several_subscriptions_each_shown_separately(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.24: несколько подписок — каждая отдельно со своим названием; продление — с
    экрана конкретной подписки."""
    first = await _subscription(shop, tariffs[0], panel_user_id=1)
    second = await _subscription(shop, tariffs[0], panel_user_id=2)
    second.name = "Для мамы"
    await shop.session.flush()

    menu = await _menu(shop)
    assert _labels(menu) == ["Мои подписки"]
    listing = await _press(shop, _button(menu, "Мои подписки"))
    labels = _labels(listing)
    assert any(label.startswith("Основная — до") for label in labels)
    assert any(label.startswith("Для мамы — до") for label in labels)

    await shop.press(USER, Card(subscription_id=second.id).pack())
    card = shop.telegram.last()
    assert "Для мамы" in _rich_text(card)
    renew = _button(card, "Продлить")
    assert renew.callback_data == Renew(subscription_id=second.id).pack()
    assert first.id != second.id


async def test_3_36_only_trial_offers_buy_instead_of_renew(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """3.36: единственная подписка — триальная: «Купить подписку» в меню и в карточке
    вместо «Продлить»; покупка применится к ней."""
    del tariffs
    trial = await _subscription(shop, None, is_trial=True)
    menu = await _menu(shop)
    assert _labels(menu) == ["Моя подписка", "Купить подписку"]
    await shop.press(USER, Card(subscription_id=trial.id).pack())
    card = shop.telegram.last()
    assert "Продлить" not in _labels(card)
    assert _button(card, "Купить подписку").callback_data == BUY


async def test_3_36_trial_disabled_in_panel_has_no_buy(shop: Shop, tariffs: list[Tariff]) -> None:
    """3.36, 4.7: триал отключён оператором в панели — «Купить подписку» нет, клиент
    видит «обратитесь в поддержку»."""
    del tariffs
    trial = await _subscription(shop, None, is_trial=True, status=PanelUserStatus.DISABLED)
    menu = await _menu(shop)
    assert "Купить подписку" not in _labels(menu)
    await shop.press(USER, Card(subscription_id=trial.id).pack())
    card = shop.telegram.last()
    assert "Купить подписку" not in _labels(card)
    assert "Напишите в поддержку" in _rich_text(card)


async def test_4_7_disabled_subscription_has_no_renew(shop: Shop, tariffs: list[Tariff]) -> None:
    """4.7: подписка отключена в панели — «Продлить» нет, клиент видит «обратитесь в
    поддержку»; продление по старой кнопке не оформляется."""
    subscription = await _subscription(shop, tariffs[0], status=PanelUserStatus.DISABLED)
    await _menu(shop)
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    card = shop.telegram.last()
    assert "Продлить" not in _labels(card)

    await shop.press(USER, Renew(subscription_id=subscription.id).pack())
    await shop.press(
        USER,
        Pay(
            tariff_id=tariffs[0].id,
            subscription_id=subscription.id,
            price=19900,
            token="old-screen",  # noqa: S106 — токен экрана подтверждения, не секрет
        ).pack(),
    )
    assert await _payments(shop) == []


# --- Закрытый магазин, тестировщики ---


@pytest.mark.usefixtures("tariffs")
async def test_1_22_paused_shop_closes_buying_but_shows_subscriptions(shop: Shop) -> None:
    """1.22: магазин временно закрыт — покупка и продление недоступны («Магазин временно
    закрыт»), свои подписки клиент видит."""
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    menu = await _menu(shop)
    screen = await _press(shop, _button(menu, "Купить подписку"))
    assert "временно не принимает новые заказы" in _text(screen)


async def test_1_22_paused_shop_still_shows_subscription_card(
    shop: Shop, tariffs: list[Tariff]
) -> None:
    """1.22: в закрытом магазине карточка подписки и ссылка доступны; «Продлить» —
    «Магазин временно закрыт»."""
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    subscription = await _subscription(shop, tariffs[0])
    await _menu(shop)
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    card = shop.telegram.last()
    assert "Скопировать ссылку" in _labels(card)
    renew = await _press(shop, _button(card, "Продлить"))
    assert "временно не принимает новые заказы" in _text(renew)


@pytest.mark.usefixtures("tariffs")
async def test_1_22_testers_buy_while_shop_is_closed(shop: Shop) -> None:
    """1.22, 1.21: для тестировщиков бот работает как обычно и в закрытом магазине."""
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    await put_setting(shop.session, SHOP_TESTERS, [USER.id])
    menu = await _menu(shop)
    choice = await _press(shop, _button(menu, "Купить подписку"))
    assert _rich(choice)


# --- Реферальная ссылка ---


async def _inviter(shop: Shop) -> Client:
    inviter = await make_client(shop.session, telegram_id=555)
    await add(shop.session, ReferralCode(code="friend42", client_id=inviter.id))
    return inviter


async def _links(shop: Shop) -> list[tuple[int, int]]:
    rows = await shop.session.execute(select(ReferralLink.invited_id, ReferralLink.inviter_id))
    return [(invited, inviter) for invited, inviter in rows]


async def test_3_15_new_client_by_referral_link_gets_link(shop: Shop) -> None:
    """3.15: новый клиент впервые открыл бот по реферальной ссылке — связь записана."""
    inviter = await _inviter(shop)
    await shop.send_text(USER, "/start friend42")
    client = await _client(shop)
    assert await _links(shop) == [(client.id, inviter.id)]


async def test_3_15_existing_client_gets_no_link(shop: Shop) -> None:
    """3.15: клиент уже существовал — связь не записывается."""
    await _inviter(shop)
    await _menu(shop)
    await shop.send_text(USER, "/start friend42")
    assert await _links(shop) == []


async def test_3_15_unknown_code_gives_no_link(shop: Shop) -> None:
    """3.15: код, которого магазин не знает, связи не даёт."""
    await shop.send_text(USER, "/start unknown-code")
    assert await _links(shop) == []


# --- Сквозной путь ---


async def test_3_6_end_to_end_purchase_from_bot_to_panel(
    shop: Shop, tariffs: list[Tariff], provider: FakeProvider
) -> None:
    """3.4, 3.6, 3.28, 3.34: клиент оплатил в боте — уведомление провайдера проверено,
    платёж применён в панели, клиент получил ссылку; переходы платежа — в журнале."""
    await _menu(shop)
    await shop.press(USER, Pick(tariff_id=tariffs[0].id, subscription_id=0).pack())
    await _press(shop, _button(shop.telegram.last(), "Оплатить"))
    [payment] = await _payments(shop)
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)

    response = await shop.http.post(
        "/webhooks/payments/fake",
        content=json.dumps({"id": payment.provider_payment_id}),
        headers={SIGNATURE: VALID_SIGNATURE},
    )
    assert response.status_code == 200

    [task] = await shop.session.scalars(select(QueueTask).where(QueueTask.name == "payments.apply"))
    context = TaskContext(session=shop.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=shop.panel, payments=ShopApplier(dev_mode=False)):
        await apply_payment.run(context, task.args)
    await shop.session.flush()

    assert payment.state == PaymentState.APPLIED
    subscription = await shop.session.get(Subscription, payment.subscription_id)
    assert subscription is not None
    assert subscription.panel_username == f"rb_{USER.id}_1"
    keys = list(
        await shop.session.scalars(
            select(QueueTask.args["text_key"].astext).where(QueueTask.name == "messages.send")
        )
    )
    assert "event.subscription_ready" in keys
    actions = [e.action for e in await entries_for(shop.session, payment_subject(payment.id))]
    assert actions == ["payment.created", "payment.paid", "payment.applied"]
