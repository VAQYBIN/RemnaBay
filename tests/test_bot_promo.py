"""Промокод в боте: скидка на экране подтверждения и дни через меню (блок 8: 8.4–8.15;
сценарий «Промокод»; решения 0065, 0066; 1.22)."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from aiogram.types import ForceReply
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._promo import PromoDays
from remnabay.bot._shop import Pay, Pick, Renew
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, PaymentState
from remnabay.domain.promo import ActivationState, PromoActivation, PromoCode, PromoKind
from remnabay.domain.subscriptions import (
    PanelUserStatus,
    SegmentKind,
    Subscription,
    TermSegment,
)
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.panel_sync._outage import PanelOutage
from remnabay.payments import ProviderStatus, ShopApplier, apply_payment
from remnabay.promo import (
    NewPromo,
    PromoLimits,
    add_promo_days,
    create_promo,
    set_disabled,
    without_payments,
)
from remnabay.queue import TaskContext
from remnabay.queue._models import QueueTask
from remnabay.shop import SHOP_STATE, ShopState
from remnabay.trial import TRIAL_ENABLED, trial_available
from tests.bot_support import AnyButton, screen_buttons, telegram_user
from tests.conftest import journaled_in_test
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
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = TeamMember(telegram_id=1, role=TeamRole.OWNER)
    await add(shop.session, member)
    return member


@pytest.fixture
async def month(shop: Shop) -> Tariff:
    tariff = make_tariff(name="Месяц", duration_days=30, price=Decimal("199.00"))
    await add(shop.session, tariff)
    return tariff


async def _promo(
    shop: Shop,
    owner: TeamMember,
    code: str,
    value: int,
    kind: PromoKind = PromoKind.PERCENT_DISCOUNT,
    *,
    max_activations: int | None = None,
) -> PromoCode:
    new = NewPromo(
        code=code, kind=kind, value=value, limits=PromoLimits(max_activations=max_activations)
    )
    return await create_promo(shop.session, new, member_id=owner.id)


async def _client(shop: Shop) -> Client:
    client = await shop.session.scalar(
        select(Client).join(TelegramAccount).where(TelegramAccount.telegram_id == USER.id)
    )
    return client or await make_client(shop.session, telegram_id=USER.id)


async def _subscription(
    shop: Shop,
    tariff: Tariff | None,
    *,
    expires_in: timedelta = timedelta(days=10),
    panel_user_id: int = 1,
    name: str = "Основная",
    is_trial: bool = False,
    status: PanelUserStatus = PanelUserStatus.ACTIVE,
) -> Subscription:
    client = await _client(shop)
    subscription = make_subscription(client, tariff, panel_user_id=panel_user_id)
    subscription.name = name
    expires_at = NOW + expires_in
    subscription.expires_at = expires_at
    subscription.is_trial = is_trial
    subscription.panel_status = status
    await add(shop.session, subscription)
    shop.panel.add_user(
        id=panel_user_id,
        username=subscription.panel_username,
        shortUuid=subscription.panel_short_uuid,
        expireAt=expires_at.isoformat(),
    )
    return subscription


def _labels(screen: Screen) -> list[str]:
    return [button.text for button in screen_buttons(screen)]


def _button(screen: Screen, text: str) -> AnyButton:
    [button] = [b for b in screen_buttons(screen) if b.text == text]
    return button


def _text(screen: Screen) -> str:
    assert not isinstance(screen, SendRichMessage)
    assert screen.text is not None
    return screen.text


async def _press(shop: Shop, button: AnyButton) -> Screen:
    assert button.callback_data is not None
    await shop.press(USER, button.callback_data)
    return shop.telegram.last()


async def _confirmation(shop: Shop, tariff: Tariff) -> Screen:
    await shop.send_text(USER, "/start")
    await shop.press(USER, Pick(tariff_id=tariff.id, subscription_id=0).pack())
    return shop.telegram.last()


async def _enter(shop: Shop, screen: Screen, code: str) -> Screen:
    """«Ввести промокод» на экране и код следующим сообщением (З8)."""
    prompt = await _press(shop, _button(screen, "Ввести промокод"))
    assert isinstance(prompt, SendMessage)
    assert prompt.text == "Пришлите промокод сообщением."
    assert isinstance(prompt.reply_markup, ForceReply)
    await shop.send_text(USER, code)
    return shop.telegram.last()


async def _menu(shop: Shop) -> Screen:
    await shop.send_text(USER, "/start")
    return shop.telegram.last()


async def _payments(shop: Shop) -> list[Payment]:
    return list(await shop.session.scalars(select(Payment).order_by(Payment.id)))


async def _activations(shop: Shop) -> list[PromoActivation]:
    return list(await shop.session.scalars(select(PromoActivation).order_by(PromoActivation.id)))


async def _apply(shop: Shop, payment: Payment) -> None:
    """Применение платежа воркером (как в сквозном тесте покупки)."""
    [task] = await shop.session.scalars(
        select(QueueTask).where(
            QueueTask.name == apply_payment.name,
            QueueTask.args["payment_id"].as_integer() == payment.id,
        )
    )
    context = TaskContext(session=shop.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=shop.panel, payments=ShopApplier(dev_mode=False)):
        await apply_payment.run(context, task.args)
    await shop.session.flush()


async def _pay(shop: Shop, provider: FakeProvider, payment: Payment) -> None:
    """Клиент оплатил: провайдер подтвердил оплату вебхуком."""
    assert payment.provider_payment_id is not None
    provider.set(payment.provider_payment_id, ProviderStatus.SUCCEEDED)
    response = await shop.http.post(
        "/webhooks/payments/fake",
        content=json.dumps({"id": payment.provider_payment_id}),
        headers={SIGNATURE: VALID_SIGNATURE},
    )
    assert response.status_code == 200


async def _journal(shop: Shop, action: str) -> list[JournalEntry]:
    return list(
        await shop.session.scalars(
            select(JournalEntry).where(journaled_in_test(), JournalEntry.action == action)
        )
    )


# --- Скидка (8.4–8.8) ---


async def test_8_4_discount_shown_before_payment(
    shop: Shop, owner: TeamMember, month: Tariff, provider: FakeProvider
) -> None:
    """8.4: код на экране подтверждения — итог пересчитан и показан до оплаты; скидка
    округлена вниз: 15% от 199 ₽ — скидка 29 ₽, итог 170 ₽. Счёт — на итог."""
    await _promo(shop, owner, "SALE15", 15)
    confirm = await _confirmation(shop, month)
    assert _labels(confirm) == ["Оплатить", "Ввести промокод", "Назад"]

    screen = await _enter(shop, confirm, "sale15")
    text = _text(screen)
    assert isinstance(screen, SendMessage)
    assert text.startswith("Промокод SALE15 применён: скидка 29,00\xa0₽.")
    assert "Цена: 199,00\xa0₽" in text
    assert "Скидка по промокоду SALE15: −29,00\xa0₽" in text
    assert "Итого: 170,00\xa0₽" in text

    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    assert (payment.amount, payment.discount_amount) == (Decimal("170.00"), Decimal("29.00"))
    [request] = provider.requests
    assert request.amount == Decimal("170.00")


async def test_8_4_total_raised_to_provider_minimum(
    shop: Shop, owner: TeamMember, provider: FakeProvider
) -> None:
    """8.4, 0065: итог больше нуля, но меньше минимальной суммы счёта у провайдера —
    поднимается до неё; экран показывает итог уже с ней."""
    provider.minimum = Decimal("50.00")
    cheap = make_tariff(name="Неделя", duration_days=7, price=Decimal("100.00"))
    await add(shop.session, cheap)
    await _promo(shop, owner, "HALF90", 90)
    screen = await _enter(shop, await _confirmation(shop, cheap), "half90")
    assert "Итого: 50,00\xa0₽" in _text(screen)
    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    assert payment.amount == Decimal("50.00")


async def test_8_5_one_promo_per_payment(shop: Shop, owner: TeamMember, month: Tariff) -> None:
    """8.5: второй код заменяет первый — к платежу применяется один промокод."""
    await _promo(shop, owner, "SALE15", 15)
    second = await _promo(shop, owner, "SALE20", 20)
    screen = await _enter(shop, await _confirmation(shop, month), "sale15")
    screen = await _enter(shop, screen, "sale20")
    assert "SALE15" not in _text(screen)
    assert "Итого: 160,00\xa0₽" in _text(screen)
    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    assert payment.promo_code_id == second.id


async def test_8_6_8_14_activation_counted_when_payment_applied(
    shop: Shop, owner: TeamMember, month: Tariff, provider: FakeProvider
) -> None:
    """8.6, 8.14: неоплаченный счёт активацию не расходует; при применении платежа
    активация засчитывается и пишется в журнал."""
    promo = await _promo(shop, owner, "SALE15", 15)
    screen = await _enter(shop, await _confirmation(shop, month), "sale15")
    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    assert await _activations(shop) == []

    await _pay(shop, provider, payment)
    await _apply(shop, payment)

    [activation] = await _activations(shop)
    assert (activation.promo_code_id, activation.payment_id, activation.state) == (
        promo.id,
        payment.id,
        ActivationState.APPLIED,
    )
    assert activation.subscription_id == payment.subscription_id
    [entry] = await _journal(shop, "promo.activated")
    assert entry.subject_id == str(promo.id)
    # Отрезок срока стоит столько, сколько заплачено (3.25)
    segment = await shop.session.scalar(
        select(TermSegment).where(TermSegment.payment_id == payment.id)
    )
    assert segment is not None
    assert segment.cost == Decimal("170.00")


async def test_8_7_promo_disabled_while_paying_keeps_discount(
    shop: Shop, owner: TeamMember, month: Tariff, provider: FakeProvider
) -> None:
    """8.7, 3.11: промокод отключили и исчерпали, пока клиент платил, — клиент получает
    скидку, которую видел; превышение лимита — в журнал."""
    promo = await _promo(shop, owner, "ONCE", 15, max_activations=1)
    screen = await _enter(shop, await _confirmation(shop, month), "once")
    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    other = await make_client(shop.session, telegram_id=5)
    await add(
        shop.session,
        PromoActivation(
            promo_code_id=promo.id,
            client_id=other.id,
            subscription_id=(await _subscription(shop, month, panel_user_id=9)).id,
            state=ActivationState.APPLIED,
        ),
    )
    await set_disabled(shop.session, promo.id, True, now=NOW, member_id=owner.id)

    await _pay(shop, provider, payment)
    await _apply(shop, payment)

    assert payment.state == PaymentState.APPLIED
    assert payment.amount == Decimal("170.00")
    assert len(await _activations(shop)) == 2
    [exceeded] = await _journal(shop, "promo.limit_exceeded")
    assert exceeded.details["activations_exhausted"] is True


async def test_8_7_promo_disabled_before_pay_shows_screen_again(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """Сквозное правило 2, 8.12: промокод отключили до нажатия «Оплатить» — платёж не
    создан, клиент видит подтверждение без скидки и нейтральную причину."""
    promo = await _promo(shop, owner, "SALE15", 15)
    screen = await _enter(shop, await _confirmation(shop, month), "sale15")
    await set_disabled(shop.session, promo.id, True, now=NOW, member_id=owner.id)

    again = await _press(shop, _button(screen, "Оплатить"))
    assert await _payments(shop) == []
    assert _text(again).startswith("Такого промокода нет или он больше не действует.")
    assert "Итого: 199,00\xa0₽" in _text(again)


async def test_8_8_full_discount_creates_zero_payment(
    shop: Shop, owner: TeamMember, month: Tariff, provider: FakeProvider
) -> None:
    """8.8: скидка 100% — итог ноль, кнопка «Получить»; платёж с нулевой суммой без
    счёта у провайдера, подписка создаётся сразу; это покупка: триал недоступен, клиент
    больше не «без оплат», выручка — ноль."""
    await put_setting(shop.session, TRIAL_ENABLED, True)
    await _promo(shop, owner, "FREE", 100)
    screen = await _enter(shop, await _confirmation(shop, month), "free")
    assert "Итого: 0,00\xa0₽" in _text(screen)
    assert _labels(screen) == ["Получить", "Ввести промокод", "Назад"]
    assert _button(screen, "Получить").style == "success"

    await _press(shop, _button(screen, "Получить"))
    [payment] = await _payments(shop)
    assert (payment.state, payment.amount, payment.provider) == (
        PaymentState.PAID,
        Decimal("0.00"),
        None,
    )
    assert provider.requests == []

    await _apply(shop, payment)
    assert payment.state == PaymentState.APPLIED
    client = await _client(shop)
    assert not await trial_available(shop.session, client)
    assert not await without_payments(shop.session, client)
    [activation] = await _activations(shop)
    assert activation.payment_id == payment.id


async def test_8_4_discount_on_renewal(shop: Shop, owner: TeamMember, month: Tariff) -> None:
    """8.4: промокод вводится и на экране продления; «Назад» после ошибки — к
    подтверждению без скидки."""
    subscription = await _subscription(shop, month)
    await _promo(shop, owner, "SALE15", 15)
    await shop.send_text(USER, "/start")
    await shop.press(USER, Renew(subscription_id=subscription.id).pack())
    confirm = shop.telegram.last()
    assert "Подписка будет действовать до" in _text(confirm)

    error = await _enter(shop, confirm, "nope")
    assert _labels(error) == ["Ввести промокод", "Назад"]
    back = await _press(shop, _button(error, "Назад"))
    assert "Итого: 199,00\xa0₽" in _text(back)

    screen = await _enter(shop, back, "sale15")
    assert "Итого: 170,00\xa0₽" in _text(screen)
    await _press(shop, _button(screen, "Оплатить"))
    [payment] = await _payments(shop)
    assert (payment.subscription_id, payment.amount) == (subscription.id, Decimal("170.00"))


# --- Ошибки и подсказки (8.9, 8.12, 8.13) ---


async def test_8_12_errors_and_continue_without_promo(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.12: нет такого кода — нейтральный текст; уже использован — причина; клиент
    может ввести другой код или продолжить без промокода."""
    screen = await _enter(shop, await _confirmation(shop, month), "nope")
    assert _text(screen) == (
        "Такого промокода нет или он больше не действует. "
        "Проверьте, нет ли опечатки, или введите другой."
    )
    assert _labels(screen) == ["Ввести промокод", "Назад"]


async def test_8_9_days_code_on_confirmation(shop: Shop, owner: TeamMember, month: Tariff) -> None:
    """8.9: код на дни на экране подтверждения — подсказка, что он вводится в меню."""
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    screen = await _enter(shop, await _confirmation(shop, month), "days7")
    assert _text(screen) == (
        "Этот промокод добавляет дни к подписке. "
        "Введите его через «Ввести промокод» в главном меню."
    )


async def test_8_9_discount_code_in_menu(shop: Shop, owner: TeamMember) -> None:
    """8.9, 0066: код на скидку, введённый через меню, — подсказка про экран
    подтверждения."""
    await _promo(shop, owner, "SALE15", 15)
    screen = await _enter(shop, await _menu(shop), "sale15")
    assert _text(screen) == (
        "Этот промокод даёт скидку. "
        "Введите его на экране подтверждения, когда будете покупать или продлевать подписку."
    )
    assert _labels(screen) == ["Ввести промокод", "Назад"]


async def test_8_13_input_limited_after_wrong_codes(shop: Shop, owner: TeamMember) -> None:
    """8.13: после 5 неверных кодов подряд ввод временно ограничен — «Слишком много
    попыток»; ввести код снова бот не предлагает."""
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    screen = await _menu(shop)
    for _ in range(5):
        screen = await _enter(shop, screen, "wrong")
    pressed = await _press(shop, _button(screen, "Ввести промокод"))
    assert _text(pressed) == "Слишком много попыток. Попробуйте ещё раз через 15 минут."
    assert _labels(pressed) == ["Назад"]


async def test_command_is_not_taken_for_code(shop: Shop) -> None:
    """З8: пока бот ждёт код, команда остаётся командой."""
    await _press(shop, _button(await _menu(shop), "Ввести промокод"))
    await shop.send_text(USER, "/start")
    assert "Ввести промокод" in _labels(shop.telegram.last())


# --- Дни (8.10, 8.11, 8.15) ---


async def test_8_10_days_added_to_single_subscription(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.10, 8.14: одна подписка — дни добавляются сразу отрезком нулевой стоимости;
    клиент видит новую дату окончания; активация — в журнале."""
    subscription = await _subscription(shop, month)
    end = subscription.expires_at
    assert end is not None
    promo = await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)

    screen = await _enter(shop, await _menu(shop), "days7")

    assert _text(screen).startswith("Готово! К подписке «Основная» добавлено 7 дней.")
    assert subscription.expires_at is not None
    assert abs(subscription.expires_at - (end + timedelta(days=7))) < timedelta(seconds=1)
    [write] = [body for path, body in shop.panel.writes if path == "PATCH /api/users"]
    assert write["id"] == subscription.panel_user_id
    segment = await shop.session.scalar(
        select(TermSegment).where(TermSegment.kind == SegmentKind.PROMO_CODE)
    )
    assert segment is not None
    assert (segment.subscription_id, segment.cost) == (subscription.id, Decimal(0))
    [activation] = await _activations(shop)
    assert (activation.state, activation.subscription_id) == (
        ActivationState.APPLIED,
        subscription.id,
    )
    [entry] = await _journal(shop, "promo.activated")
    assert entry.subject_id == str(promo.id)
    assert len(await _journal(shop, "subscription.days_added")) == 1


async def test_8_10_expired_subscription_counts_from_activation(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.10: подписка истекла — дни отсчитываются от момента активации."""
    subscription = await _subscription(shop, month, expires_in=-timedelta(days=5))
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    before = datetime.now(UTC)
    await _enter(shop, await _menu(shop), "days7")
    assert subscription.expires_at is not None
    assert subscription.expires_at >= before + timedelta(days=7) - timedelta(seconds=1)
    assert subscription.expires_at <= datetime.now(UTC) + timedelta(days=7)


async def test_8_10_several_subscriptions_client_chooses(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.10: подписок несколько — клиент выбирает, к какой добавить дни; триальная
    подходит, отключённая в панели — нет."""
    await _subscription(shop, month, name="Основная")
    trial = await _subscription(shop, None, panel_user_id=2, name="Пробная", is_trial=True)
    await _subscription(
        shop, month, panel_user_id=3, name="Отключённая", status=PanelUserStatus.DISABLED
    )
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)

    choice = await _enter(shop, await _menu(shop), "days7")
    assert _text(choice) == "К какой подписке добавить 7 дней?"
    assert _labels(choice) == ["Основная", "Пробная", "Назад"]

    result = await _press(shop, _button(choice, "Пробная"))
    assert isinstance(result, EditMessageText)
    assert "К подписке «Пробная» добавлено 7 дней" in _text(result)
    [activation] = await _activations(shop)
    assert activation.subscription_id == trial.id


async def test_8_2_one_activation_per_client(shop: Shop, owner: TeamMember, month: Tariff) -> None:
    """8.2, 8.12: одна активация на клиента — второй раз «Вы уже использовали»."""
    await _subscription(shop, month)
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    screen = await _enter(shop, await _menu(shop), "days7")
    screen = await _enter(shop, await _menu(shop), "DAYS7")
    assert _text(screen) == "Вы уже использовали этот промокод."


async def test_8_11_days_without_subscription(shop: Shop, owner: TeamMember) -> None:
    """8.11: подписок нет — код не активируется, клиент видит, что сначала нужно
    оформить подписку."""
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    screen = await _enter(shop, await _menu(shop), "days7")
    assert _text(screen) == (
        "Этот промокод добавляет дни к подписке. Сначала оформите подписку, а потом возвращайтесь."
    )
    assert await _activations(shop) == []


async def test_8_15_days_queued_while_panel_unavailable(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.15: панель недоступна — «Дни будут добавлены в ближайшие минуты»; после
    применения клиент получает подтверждение."""
    subscription = await _subscription(shop, month)
    end = subscription.expires_at
    assert end is not None
    await add(shop.session, PanelOutage(started_at=NOW))
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)

    screen = await _enter(shop, await _menu(shop), "days7")
    assert _text(screen) == (
        "Промокод принят. Дни будут добавлены к подписке «Основная» в ближайшие минуты — "
        "мы пришлём подтверждение."
    )
    [activation] = await _activations(shop)
    assert activation.state == ActivationState.PENDING
    [task] = await shop.session.scalars(
        select(QueueTask).where(QueueTask.name == add_promo_days.name)
    )
    assert task.key == f"subscription:{subscription.id}"

    context = TaskContext(session=shop.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=shop.panel):
        await add_promo_days.run(context, task.args)
    await shop.session.flush()

    assert activation.state == ActivationState.APPLIED
    assert subscription.expires_at is not None
    assert abs(subscription.expires_at - (end + timedelta(days=7))) < timedelta(seconds=1)
    keys = list(
        await shop.session.scalars(
            select(QueueTask.args["text_key"].astext).where(QueueTask.name == "messages.send")
        )
    )
    assert "promo.days_applied" in keys


async def test_8_15_repeated_task_adds_days_once(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """4.15, сквозное правило 3: повтор операции не добавляет дни второй раз."""
    subscription = await _subscription(shop, month)
    end = subscription.expires_at
    assert end is not None
    await add(shop.session, PanelOutage(started_at=NOW))
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    await _enter(shop, await _menu(shop), "days7")
    [task] = await shop.session.scalars(
        select(QueueTask).where(QueueTask.name == add_promo_days.name)
    )
    context = TaskContext(session=shop.session, task_id=task.id, attempt_number=1)
    with fake_runtime(panel=shop.panel):
        await add_promo_days.run(context, task.args)
        await add_promo_days.run(context, task.args)
    await shop.session.flush()
    assert subscription.expires_at is not None
    assert abs(subscription.expires_at - (end + timedelta(days=7))) < timedelta(seconds=1)


async def test_1_22_days_promo_works_in_paused_shop(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """1.22: пока магазин временно закрыт, промокод на дни работает."""
    await _subscription(shop, month)
    await _promo(shop, owner, "DAYS7", 7, PromoKind.DAYS)
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    screen = await _enter(shop, await _menu(shop), "days7")
    assert _text(screen).startswith("Готово!")


# --- Подделанные данные кнопок ---


async def test_forged_pay_button_gives_no_discount(
    shop: Shop, owner: TeamMember, month: Tariff
) -> None:
    """8.13: номер промокода в «Оплатить» клиент может подделать — скидку даёт только
    код, который он ввёл сам; подбор по номерам не обходит защиту от перебора."""
    secret = await _promo(shop, owner, "SECRET", 100)
    confirm = await _confirmation(shop, month)
    data = _button(confirm, "Оплатить").callback_data
    assert data is not None
    forged = Pay.unpack(data).model_copy(update={"promo": secret.id, "price": 0})

    screen = await _press(shop, AnyButton(text="Оплатить", callback_data=forged.pack()))
    assert await _payments(shop) == []
    assert "SECRET" not in _text(screen)
    assert "Итого: 199,00\xa0₽" in _text(screen)


async def test_forged_days_choice_is_rejected(shop: Shop, owner: TeamMember, month: Tariff) -> None:
    """8.10, 8.13: выбор подписки для дней принимает только введённый клиентом код."""
    subscription = await _subscription(shop, month)
    secret = await _promo(shop, owner, "SECRET", 30, PromoKind.DAYS)
    forged = PromoDays(promo_id=secret.id, subscription_id=subscription.id)
    await shop.send_text(USER, "/start")
    screen = await _press(shop, AnyButton(text="x", callback_data=forged.pack()))
    assert _text(screen).startswith("Такого промокода нет")
    assert await _activations(shop) == []
