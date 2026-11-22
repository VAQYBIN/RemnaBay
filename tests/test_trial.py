"""Триал: настройки, предложение, выдача, повтор, недоступная панель, трафик (блок 5;
5.1–5.6, 5.10, 1.22, 4.32; решения 0013, 0035, 0058)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._common import BUY, TRY_FREE, Card
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import PaymentState
from remnabay.domain.subscriptions import PanelUserStatus, SegmentKind, Subscription, TermSegment
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.domain.trials import Trial
from remnabay.journal import Subject, entries_for
from remnabay.messaging import SendArgs, send_message
from remnabay.panel import PanelUnavailableError
from remnabay.panel_sync import ReconcileArgs, Source, reconcile
from remnabay.queue import Ending, TaskContext
from remnabay.shop import SHOP_STATE, TRIAL_ENABLED, ShopState
from remnabay.trial import (
    GIGABYTE,
    TRIAL_SQUADS,
    TrialArgs,
    create_trial,
    trial_processing_notice,
)
from tests.bot_support import AnyButton, screen_buttons, telegram_user
from tests.domain_support import add, make_client, make_payment, make_subscription, put_setting
from tests.guide_support import queued, run_queued
from tests.panel_support import fake_runtime
from tests.web_support import Shop, running_shop

USER = telegram_user(777)
SQUAD = "6f0b2c3e-1d2a-4b5c-8d9e-0f1a2b3c4d5e"

type Screen = SendMessage | SendRichMessage | EditMessageText


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        shop.panel.squads = {SQUAD: "Триал"}
        yield shop


async def _enable(shop: Shop) -> None:
    await put_setting(shop.session, TRIAL_ENABLED, True)
    await put_setting(shop.session, TRIAL_SQUADS, [UUID(SQUAD)])


def _labels(screen: Screen) -> list[str]:
    return [button.text for button in screen_buttons(screen)]


def _button(screen: Screen, text: str) -> AnyButton:
    [button] = [b for b in screen_buttons(screen) if b.text == text]
    return button


async def _menu(shop: Shop) -> Screen:
    await shop.send_text(USER, "/start")
    return shop.telegram.last()


async def _client(shop: Shop) -> Client:
    client = await shop.session.scalar(
        select(Client).join(TelegramAccount).where(TelegramAccount.telegram_id == USER.id)
    )
    assert client is not None
    return client


async def _messages(shop: Shop) -> list[SendArgs]:
    return [SendArgs.model_validate(task.args) for task in await queued(shop.session, send_message)]


async def _issue(shop: Shop) -> None:
    with fake_runtime(panel=shop.panel):
        await run_queued(shop.session, create_trial)


# --- 5.1. Настройки ---


async def _owner_session(shop: Shop) -> None:
    owner = await shop.session.scalar(select(TeamMember).where(TeamMember.telegram_id == 1))
    assert owner is not None
    await shop.sign_in(owner)


async def test_5_1_trial_settings_defaults_and_save(shop: Shop) -> None:
    """5.1: владелец видит значения по умолчанию (3 дня, 10 ГБ, 1 устройство), включает
    триал со сквадом из панели; изменения — в журнале."""
    await _owner_session(shop)
    response = await shop.http.get("/api/admin/settings/trial")
    assert response.json() == {
        "enabled": False,
        "duration_days": 3,
        "traffic_limit_gb": 10,
        "device_limit": 1,
        "squad_uuids": [],
    }
    body = {
        "enabled": True,
        "duration_days": 5,
        "traffic_limit_gb": 0,
        "device_limit": 2,
        "squad_uuids": [SQUAD],
    }
    saved = await shop.http.put("/api/admin/settings/trial", json=body)
    assert saved.status_code == 200
    assert saved.json() == body
    changed = await entries_for(shop.session, Subject("setting", "trial.enabled"))
    assert [entry.action for entry in changed] == ["setting.changed"]


async def test_5_1_enabling_requires_known_squad(shop: Shop) -> None:
    """5.1, 04-operator-settings: при включении сквад обязателен и должен быть в панели."""
    await _owner_session(shop)
    base = {"enabled": True, "duration_days": 3, "traffic_limit_gb": 10, "device_limit": 1}
    empty = await shop.http.put("/api/admin/settings/trial", json={**base, "squad_uuids": []})
    assert empty.status_code == 422
    unknown = await shop.http.put(
        "/api/admin/settings/trial",
        json={**base, "squad_uuids": ["00000000-0000-0000-0000-00000000abcd"]},
    )
    assert unknown.status_code == 422
    assert not (await shop.http.get("/api/admin/settings/trial")).json()["enabled"]


# --- 5.2, 5.3. Предложение ---


async def test_5_2_offer_in_main_menu_and_under_tariffs(shop: Shop) -> None:
    """5.2: триал доступен — «Попробовать бесплатно» (зелёная) в главном меню и внизу
    экрана выбора тарифа; «Купить подписку» тогда не зелёная."""
    await _enable(shop)
    menu = await _menu(shop)
    assert _labels(menu) == [
        "Попробовать бесплатно",
        "Купить подписку",
        "Ввести промокод",
    ]
    assert _button(menu, "Попробовать бесплатно").style == "success"
    assert _button(menu, "Купить подписку").style is None

    await shop.press(USER, BUY)
    choice = shop.telegram.last()
    assert _labels(choice)[-2:] == ["Попробовать бесплатно", "Назад"]


async def test_5_2_no_offer_when_trial_disabled(shop: Shop) -> None:
    """5.2: триал выключен — предложения нет, «Купить подписку» — целевое действие."""
    menu = await _menu(shop)
    assert _labels(menu) == ["Купить подписку", "Ввести промокод"]
    assert _button(menu, "Купить подписку").style == "success"


@pytest.mark.parametrize("reason", ["used", "paid", "adopted"])
async def test_5_3_no_trial_after_trial_payment_or_adoption(shop: Shop, reason: str) -> None:
    """5.3: клиент уже получал триал, хотя бы раз платил или усыновлён — предложения нет,
    а нажатие старой кнопки триал не создаёт."""
    await _enable(shop)
    await _menu(shop)
    client = await _client(shop)
    match reason:
        case "used":
            await add(shop.session, Trial(client_id=client.id))
        case "paid":
            payment = make_payment(client, None, state=PaymentState.APPLIED)
            payment.paid_at = datetime.now(UTC)
            await add(shop.session, payment)
        case _:
            client.adopted_at = datetime.now(UTC)
            await shop.session.flush()

    assert "Попробовать бесплатно" not in _labels(await _menu(shop))
    await shop.press(USER, TRY_FREE)
    assert await queued(shop.session, create_trial) == []


# --- 5.4–5.6. Выдача ---


async def test_5_4_trial_creates_panel_user_link_and_marks_trial_used(shop: Shop) -> None:
    """5.4: «Попробовать бесплатно» — пользователь панели с параметрами триала, ссылка и
    инструкция клиенту (С3), факт использования закреплён за клиентом."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    assert "Попробовать бесплатно" not in _labels(shop.telegram.last())

    await _issue(shop)

    client = await _client(shop)
    [(_, body)] = [write for write in shop.panel.writes if write[0] == "POST /api/users"]
    assert body["username"] == "rb_777_1"
    assert body["trafficLimitBytes"] == 10 * GIGABYTE
    assert body["trafficLimitStrategy"] == "NO_RESET"
    assert body["hwidDeviceLimit"] == 1
    assert body["activeInternalSquads"] == [SQUAD]
    assert body["telegramId"] == USER.id
    expire = datetime.fromisoformat(body["expireAt"])
    assert abs(expire - (datetime.now(UTC) + timedelta(days=3))) < timedelta(minutes=1)

    [subscription] = await shop.session.scalars(
        select(Subscription).where(Subscription.client_id == client.id)
    )
    assert (subscription.is_trial, subscription.tariff_id) == (True, None)
    trial = await shop.session.get(Trial, client.id)
    assert trial is not None
    assert trial.subscription_id == subscription.id
    [segment] = await shop.session.scalars(
        select(TermSegment).where(TermSegment.subscription_id == subscription.id)
    )
    assert (segment.kind, segment.cost) == (SegmentKind.TRIAL, 0)

    [message] = await _messages(shop)
    assert message.text_key == "event.trial_activated"
    assert message.variables["trial_period"] == "3 дня"
    assert message.variables["subscription_link"] == subscription.subscription_url
    assert message.dates == {"end_date": subscription.expires_at}
    assert [(b.text_key, b.copy_text, b.callback_data) for b in message.buttons] == [
        ("btn.copy_link", subscription.subscription_url, None),
        ("btn.how_to_connect", None, f"guide:{subscription.id}"),
        ("btn.main_menu", None, "menu:main"),
    ]
    issued = await entries_for(shop.session, Subject("client", client.id))
    assert "trial.issued" in [entry.action for entry in issued]


async def test_5_5_repeated_press_creates_one_trial(shop: Shop) -> None:
    """5.5: повторное нажатие — пока триал создаётся и после — второй не создаётся."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    await shop.press(USER, TRY_FREE)
    assert len(await queued(shop.session, create_trial)) == 1

    await _issue(shop)
    await shop.press(USER, TRY_FREE)
    assert await queued(shop.session, create_trial) == []
    users = [write for write in shop.panel.writes if write[0] == "POST /api/users"]
    assert len(users) == 1


async def test_5_5_task_repeated_does_not_create_second_user(shop: Shop) -> None:
    """5.5, сквозное правило 3: повтор задачи после выдачи — ничего второй раз."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    [task] = await queued(shop.session, create_trial)
    await _issue(shop)
    with fake_runtime(panel=shop.panel):
        await create_trial.run(TaskContext(shop.session, task.id, 2), task.args)
    assert len([w for w in shop.panel.writes if w[0] == "POST /api/users"]) == 1


async def test_5_6_panel_down_client_gets_processing_and_trial_waits(shop: Shop) -> None:
    """5.6: панель недоступна — клиент получает «Создаём пробную подписку» (С18),
    создание повторяется; триал выдан только после создания пользователя в панели."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    client = await _client(shop)
    shop.panel.down = True
    with fake_runtime(panel=shop.panel), pytest.raises(PanelUnavailableError):
        await run_queued(shop.session, create_trial)
    assert await shop.session.get(Trial, client.id) is None

    with fake_runtime(panel=shop.panel):
        await run_queued(shop.session, trial_processing_notice)
    [message] = await _messages(shop)
    assert message.text_key == "event.trial_processing"

    shop.panel.down = False
    await _issue(shop)
    assert await shop.session.get(Trial, client.id) is not None


async def test_5_6_no_processing_message_when_trial_is_ready(shop: Shop) -> None:
    """5.6, 4.19: триал создан быстро — «Создаём пробную подписку» не приходит."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    await _issue(shop)
    with fake_runtime(panel=shop.panel):
        await run_queued(shop.session, trial_processing_notice)
    assert [m.text_key for m in await _messages(shop)] == ["event.trial_activated"]


async def test_4_32_cancelled_trial_is_not_used(shop: Shop) -> None:
    """4.32: команда отменила создание триала — клиент получает «Не удалось выполнить
    действие», триал не считается использованным."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    [task] = await queued(shop.session, create_trial)
    client = await _client(shop)

    await create_trial.ended(Ending.CANCELLED, shop.session, task.id, task.args)

    assert [m.text_key for m in await _messages(shop)] == ["event.action_failed"]
    assert await shop.session.get(Trial, client.id) is None
    assert TrialArgs.model_validate(task.args).client_id == client.id


# --- 1.22. Магазин временно закрыт ---


async def test_1_22_paused_shop_answers_shop_paused_on_trial(shop: Shop) -> None:
    """1.22, решение 0058: пока магазин закрыт, «Попробовать бесплатно» отвечает «Магазин
    временно закрыт», триал не создаётся."""
    await _enable(shop)
    await _menu(shop)
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    await shop.press(USER, TRY_FREE)
    screen = shop.telegram.last()
    assert isinstance(screen, EditMessageText)
    assert "временно не принимает новые заказы" in (screen.text or "")
    assert await queued(shop.session, create_trial) == []


# --- 5.10. Трафик триала ---


async def _trial_subscription(session: AsyncSession) -> Subscription:
    client = await make_client(session, telegram_id=USER.id)
    subscription = make_subscription(client, None, panel_user_id=7)
    subscription.is_trial = True
    subscription.panel_short_uuid = "abc123"
    subscription.subscription_url = "https://sub.example.com/abc123"
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = datetime(2026, 11, 5, 12, tzinfo=UTC)
    await add(session, subscription)
    return subscription


async def _reconcile_limited(shop: Shop, subscription: Subscription) -> None:
    shop.panel.add_user(id=7, status="LIMITED", trafficLimitBytes=10 * GIGABYTE)
    with fake_runtime(panel=shop.panel):
        args = ReconcileArgs(subscription_id=subscription.id, source=Source.WEBHOOK)
        await reconcile(shop.session, subscription, args)
    await shop.session.flush()


async def test_5_10_trial_traffic_exhausted_message_once(shop: Shop) -> None:
    """5.10: трафик триала исчерпан (событие панели) — «Трафик пробного периода
    закончился» с «Купить подписку»; повторная сверка второго сообщения не шлёт."""
    subscription = await _trial_subscription(shop.session)
    await _reconcile_limited(shop, subscription)
    await _reconcile_limited(shop, subscription)

    [message] = await _messages(shop)
    assert message.text_key == "event.trial_traffic_exhausted"
    assert [(b.text_key, b.callback_data, b.style) for b in message.buttons] == [
        ("btn.buy", "buy", "success"),
        ("btn.main_menu", "menu:main", None),
    ]


async def test_5_10_paid_subscription_gets_no_trial_message(shop: Shop) -> None:
    """5.10: сообщение — только о триале."""
    subscription = await _trial_subscription(shop.session)
    subscription.is_trial = False
    await _reconcile_limited(shop, subscription)
    assert await _messages(shop) == []


async def test_1_22_paused_shop_keeps_trial_traffic_message(shop: Shop) -> None:
    """1.22, решение 0058: пока магазин закрыт, «Трафик пробного периода закончился» не
    отправляется."""
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    subscription = await _trial_subscription(shop.session)
    await _reconcile_limited(shop, subscription)
    assert await _messages(shop) == []


async def test_5_3_payment_while_trial_waits_skips_trial(shop: Shop) -> None:
    """5.3, 5.6: пока создание триала ждало панель, клиент оплатил покупку — триал не
    создаётся, второй подписки нет."""
    await _enable(shop)
    await _menu(shop)
    await shop.press(USER, TRY_FREE)
    client = await _client(shop)
    payment = make_payment(client, None, state=PaymentState.APPLIED)
    payment.paid_at = datetime.now(UTC)
    await add(shop.session, payment)

    await _issue(shop)

    assert not [write for write in shop.panel.writes if write[0] == "POST /api/users"]
    assert await shop.session.get(Trial, client.id) is None


async def test_0060_trial_card_shows_trial_as_tariff(shop: Shop) -> None:
    """0060: у триальной подписки в карточке «Тариф» — «Пробный период»."""
    subscription = await _trial_subscription(shop.session)
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    screen = shop.telegram.last()
    assert isinstance(screen, EditMessageText)
    assert screen.rich_message is not None
    content = screen.rich_message.model_dump_json()
    assert "Пробный период" in content
    assert "Не выбран" not in content
