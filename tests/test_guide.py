"""Инструкция по подключению из конфига Subscription Page (блок 12; 12.1–12.9, 12.16, 12.17;
3.6; /help; решения 0019, 0043, 0058)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from aiogram.types import (
    InlineKeyboardMarkup,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichMessage,
    RichMessageButton,
    RichTextBold,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._common import Card, Guide, GuideApps, GuideBack, GuideHere
from remnabay.domain.clients import Client
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.guide import (
    BROWSER_HEADERS,
    DEFAULT_CONFIG_UUID,
    WEBPAGE_ALLOWED,
    RedirectLink,
    StoredConfig,
    refresh_configs_task,
)
from remnabay.guide._store import RefreshArgs
from remnabay.messaging import guide_button
from remnabay.panel_sync._outage import PanelOutage
from remnabay.shop import SHOP_STATE, SHOP_SUPPORT_CONTACT, TRIAL_ENABLED, ShopState
from remnabay.trial import TRIAL_SQUADS
from tests.bot_support import screen_buttons, telegram_user
from tests.domain_support import add, make_client, make_subscription, put_setting
from tests.guide_support import SQUAD_CONFIG, guide_config, run_queued
from tests.panel_support import DEFAULT_CONFIG, fake_runtime
from tests.web_support import Shop, running_shop

USER = telegram_user(777)
# 0060: /help перечисляет команды бота
COMMANDS = "Команды:\n/start — главное меню\n/help — помощь и инструкция по подключению"
USER_EN = telegram_user(777, language_code="en")
USER_DE = telegram_user(777, language_code="de")
SHOP = "https://shop.example.com"

type Screen = SendMessage | SendRichMessage | EditMessageText


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        shop.panel.subpage_configs[DEFAULT_CONFIG] = guide_config()
        yield shop


async def _subscription(
    shop: Shop, *, client: Client | None = None, user_id: int = 7, name: str = "Основная"
) -> Subscription:
    if client is None:
        client = await make_client(shop.session, telegram_id=USER.id)
        client.language_code = "ru"
    subscription = make_subscription(client, None, panel_user_id=user_id)
    subscription.name = name
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = datetime(2026, 12, 1, tzinfo=UTC)
    await add(shop.session, subscription)
    return subscription


async def _guide(shop: Shop, subscription: Subscription) -> Screen:
    await shop.press(USER, Guide(subscription_id=subscription.id).pack())
    return shop.telegram.last()


def _rich(screen: Screen) -> InputRichMessage:
    assert not isinstance(screen, SendMessage)
    assert screen.rich_message is not None
    return screen.rich_message


def _platforms(screen: Screen) -> dict[str, InputRichBlockDetails]:
    """Сворачиваемые блоки платформ по названию."""
    found: dict[str, InputRichBlockDetails] = {}
    for block in _rich(screen).blocks or []:
        if isinstance(block, InputRichBlockDetails):
            assert isinstance(block.summary, RichTextBold)
            assert isinstance(block.summary.text, str)
            found[block.summary.text] = block
    return found


def _rich_buttons(block: InputRichBlockDetails) -> list[RichMessageButton]:
    return [
        button
        for inner in block.blocks
        if isinstance(inner, InputRichBlockButtons)
        for button in inner.buttons
    ]


def _labels(screen: Screen) -> list[str]:
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


def _text(screen: Screen) -> str:
    assert isinstance(screen, SendMessage)
    return screen.text or ""


def _json(screen: Screen) -> str:
    return _rich(screen).model_dump_json(exclude_defaults=True)


# --- С2. Инструкция ---


async def test_12_1_default_config_when_panel_names_none(shop: Shop) -> None:
    """12.1, 0043: панель не назвала конфиг (нет внешнего сквада или у сквада нет
    конфига) — инструкция из конфига по умолчанию; конфиг сохраняется (12.7)."""
    subscription = await _subscription(shop)
    screen = await _guide(shop, subscription)
    assert isinstance(screen, SendRichMessage)
    assert set(_platforms(screen)) == {"iOS", "Linux"}
    assert subscription.subpage_config_uuid == DEFAULT_CONFIG_UUID
    assert await shop.session.get(StoredConfig, DEFAULT_CONFIG_UUID) is not None


async def test_12_1_squad_config_is_used(shop: Shop) -> None:
    """12.1: у внешнего сквада пользователя свой конфиг — инструкция из него."""
    subscription = await _subscription(shop)
    shop.panel.user_configs[subscription.panel_short_uuid] = SQUAD_CONFIG
    shop.panel.subpage_configs[SQUAD_CONFIG] = guide_config(
        platforms={"android": {"displayName": {"ru": "Android"}, "apps": [{"name": "Happ"}]}}
    )
    assert set(_platforms(await _guide(shop, subscription))) == {"Android"}


async def test_12_2_language_client_then_default_then_first(shop: Shop) -> None:
    """12.2: тексты на языке клиента; нет его — на языке бота по умолчанию; нет и его —
    на первом языке конфига."""
    subscription = await _subscription(shop)
    await shop.press(USER_EN, Guide(subscription_id=subscription.id).pack())
    assert "App installation" in _json(shop.telegram.last())

    await shop.press(USER_DE, Guide(subscription_id=subscription.id).pack())
    assert "Установка приложения" in _json(shop.telegram.last())

    shop.panel.subpage_configs[DEFAULT_CONFIG] = guide_config(
        locales=["fr"],
        platforms={
            "ios": {
                "displayName": {"fr": "iOS-fr", "zh": "iOS-zh"},
                "apps": [{"name": "Happ", "blocks": []}],
            }
        },
    )
    await shop.session.delete(await shop.session.get(StoredConfig, DEFAULT_CONFIG_UUID))
    await shop.session.flush()
    assert set(_platforms(await _guide(shop, subscription))) == {"iOS-fr"}


async def test_12_3_recommended_app_and_other_apps(shop: Shop) -> None:
    """12.3, 0058: в блоке платформы — первое рекомендуемое приложение и его шаги;
    остальные (включая другие рекомендуемые) — по «Другие приложения»; без
    рекомендуемых — первое приложение платформы."""
    subscription = await _subscription(shop)
    screen = await _guide(shop, subscription)
    platforms = _platforms(screen)
    ios = platforms["iOS"].model_dump_json()
    assert "Happ" in ios
    assert "Stash" not in ios
    assert "FlClashX" in platforms["Linux"].model_dump_json()
    more = [b for b in _rich_buttons(platforms["iOS"]) if b.text == "Другие приложения"]
    assert [b.callback_data for b in more] == [
        GuideApps(subscription_id=subscription.id, platform="ios").pack()
    ]

    await shop.press(USER, GuideApps(subscription_id=subscription.id, platform="ios").pack())
    expanded = shop.telegram.last()
    assert isinstance(expanded, EditMessageText)
    opened = _platforms(expanded)["iOS"]
    assert opened.is_open
    content = opened.model_dump_json()
    assert content.index("Happ") < content.index("Stash") < content.index("Streisand")
    assert "Другие приложения" not in content


async def test_12_4_step_buttons_copy_link_and_redirect(shop: Shop) -> None:
    """12.4, 12.10, 12.11: копирование — ссылка подписки; внешняя ссылка — как есть;
    импорт в приложение — страница-переходник с токеном, без ссылки подписки в адресе."""
    subscription = await _subscription(shop)
    buttons = {
        b.text: b for b in _rich_buttons(_platforms(await _guide(shop, subscription))["iOS"])
    }
    assert buttons["Открыть в App Store"].url == "https://apps.apple.com/app/happ"
    copy = buttons["Скопировать"].copy_text
    assert copy is not None
    assert copy.text == subscription.subscription_url
    url = buttons["Добавить подписку"].url
    assert url is not None
    assert url.startswith(f"{SHOP}/connect/")
    assert subscription.subscription_url not in url
    assert subscription.panel_short_uuid not in url
    [link] = await shop.session.scalars(select(RedirectLink).where(RedirectLink.app_name == "Happ"))
    assert url == f"{SHOP}/connect/{link.token}"

    # Повторное открытие инструкции — тот же токен
    again = _rich_buttons(_platforms(await _guide(shop, subscription))["iOS"])
    assert [b.url for b in again if b.text == "Добавить подписку"] == [url]


async def test_12_5_no_svg_or_web_branding(shop: Shop) -> None:
    """12.5: SVG-иконки и брендинг веб-страницы из конфига в боте не используются."""
    content = _json(await _guide(shop, await _subscription(shop)))
    assert "svg" not in content.lower()
    assert "logo.svg" not in content


async def test_12_6_open_in_browser_only_when_panel_allows(shop: Shop) -> None:
    """12.6, 12.16: «Открыть в браузере» ведёт на ссылку подписки и показывается, только
    если панель пускает браузер; спрашивается с типичными заголовками браузера."""
    subscription = await _subscription(shop)
    screen = await _guide(shop, subscription)
    assert _labels(screen) == ["Скопировать ссылку", "Открыть в браузере", "Главное меню"]
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[1][0].url == subscription.subscription_url
    assert shop.panel.subpage_headers == BROWSER_HEADERS

    shop.panel.webpage_allowed = False
    assert _labels(await _guide(shop, subscription)) == ["Скопировать ссылку", "Главное меню"]


async def test_12_8_panel_down_uses_saved_config(shop: Shop) -> None:
    """12.8: панель недоступна — инструкция из последнего сохранённого конфига."""
    subscription = await _subscription(shop)
    await _guide(shop, subscription)
    shop.panel.down = True
    assert set(_platforms(await _guide(shop, subscription))) == {"iOS", "Linux"}


@pytest.mark.parametrize(
    ("allowed", "text", "labels"),
    [
        (
            None,
            "Инструкция временно недоступна. Скопируйте ссылку и откройте её в браузере",
            ["Скопировать ссылку", "Открыть в браузере", "Главное меню"],
        ),
        (
            False,
            "Инструкция временно недоступна. Скопируйте ссылку и добавьте её в приложение",
            ["Скопировать ссылку", "Главное меню"],
        ),
    ],
)
async def test_12_8_12_16_never_fetched_config_shows_unavailable(
    shop: Shop, allowed: bool | None, text: str, labels: list[str]
) -> None:
    """12.8, 12.16: конфиг ни разу не получен — `guide.unavailable` с «Скопировать
    ссылку» и «Открыть в браузере»; если панель не пускает браузер —
    `guide.unavailable_no_browser` только с копированием. Ответа о браузере не было —
    считается, что пускает."""
    subscription = await _subscription(shop)
    await put_setting(shop.session, WEBPAGE_ALLOWED, allowed)
    shop.panel.down = True
    screen = await _guide(shop, subscription)
    assert _text(screen).startswith(text)
    assert _labels(screen) == labels


async def test_3_6_subscription_ready_offers_guide(shop: Shop) -> None:
    """3.6, 4.29, С1, С15: в «Подписка готова» и «Ссылка обновлена» — «Как подключить»;
    кнопка открывает инструкцию этой подписки."""
    subscription = await _subscription(shop)
    button = guide_button(subscription.id)
    data = Guide(subscription_id=subscription.id).pack()
    assert button.callback_data == data
    await shop.press(USER, data)
    assert isinstance(shop.telegram.last(), SendRichMessage)


async def test_12_card_has_how_to_connect(shop: Shop) -> None:
    """З3: в карточке подписки — «Как подключить»."""
    subscription = await _subscription(shop)
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    labels = [button.text for button in screen_buttons(shop.telegram.last())]
    assert "Как подключить" in labels


# --- 12.7, 12.17. Обновление конфига, админка ---


async def _owner(shop: Shop) -> None:
    owner = await shop.session.scalar(select(TeamMember).where(TeamMember.telegram_id == 1))
    assert owner is not None
    await shop.sign_in(owner)


async def test_12_7_refresh_now_and_periodically(shop: Shop) -> None:
    """12.7: «Обновить сейчас» в админке и периодическое обновление получают конфиг
    заново; правка в панели видна в инструкции без перезапуска."""
    await _owner(shop)
    subscription = await _subscription(shop)
    before = (await shop.http.get("/api/admin/settings/panel")).json()
    assert before["guide_refreshed_at"] is None

    refreshed = await shop.http.post("/api/admin/settings/panel/guide/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["guide_refreshed_at"] is not None

    shop.panel.subpage_configs[DEFAULT_CONFIG] = guide_config(
        platforms={"windows": {"displayName": {"ru": "Windows"}, "apps": [{"name": "Happ"}]}}
    )
    with fake_runtime(panel=shop.panel):
        await refresh_configs_task.enqueue(shop.session, RefreshArgs())
        await run_queued(shop.session, refresh_configs_task)
    shop.panel.down = True
    assert set(_platforms(await _guide(shop, subscription))) == {"Windows"}


async def test_12_7_refresh_now_when_panel_down(shop: Shop) -> None:
    """12.7, 12.8: панель недоступна — «Обновить сейчас» сообщает об этом, действует
    последний сохранённый конфиг."""
    await _owner(shop)
    shop.panel.down = True
    response = await shop.http.post("/api/admin/settings/panel/guide/refresh")
    assert response.status_code == 503


async def test_12_17_panel_settings_report_browser_blocked(shop: Shop) -> None:
    """12.16, 12.17: обновление узнаёт у панели, пускает ли она браузер; ответ виден на
    экране «Панель» — по нему админка показывает предупреждение."""
    await _owner(shop)
    await _subscription(shop)
    shop.panel.webpage_allowed = False
    response = await shop.http.post("/api/admin/settings/panel/guide/refresh")
    assert response.json()["webpage_allowed"] is False


async def test_12_9_no_guide_fields_in_admin(shop: Shop) -> None:
    """12.9: своих полей инструкции в админке нет — в API нет настроек текста шагов."""
    await _owner(shop)
    panel: dict[str, Any] = (await shop.http.get("/api/admin/settings/panel")).json()
    assert {key for key in panel if key.startswith("guide")} == {"guide_refreshed_at"}


# --- /help ---


async def _help(shop: Shop) -> Screen:
    await shop.send_text(USER, "/help")
    return shop.telegram.last()


async def test_help_one_subscription(shop: Shop) -> None:
    """/help, 0058: одна подписка — текст с контактом поддержки и «Как подключить»."""
    await put_setting(shop.session, SHOP_SUPPORT_CONTACT, "@support")
    await _subscription(shop)
    screen = await _help(shop)
    assert isinstance(screen, SendMessage)
    assert _text(screen) == (
        "Инструкция по подключению — по кнопке ниже. Если что-то не получается, напишите "
        f"в поддержку: @support.\n\n{COMMANDS}"
    )
    assert _labels(screen) == ["Как подключить", "Главное меню"]


async def test_help_several_subscriptions(shop: Shop) -> None:
    """/help, 0058: несколько подписок — по кнопке на каждую с её названием."""
    first = await _subscription(shop)
    client = await shop.session.get(Client, first.client_id)
    assert client is not None
    await _subscription(shop, client=client, user_id=8, name="Ноутбук")
    screen = await _help(shop)
    assert _text(screen) == f"Инструкция по подключению — по кнопке ниже.\n\n{COMMANDS}"
    assert _labels(screen) == ["Основная", "Ноутбук", "Главное меню"]


async def test_help_without_subscription_offers_purchase_and_trial(shop: Shop) -> None:
    """/help, 0058: подписок нет — `help.no_subscription` и «Купить подписку»; если
    триал доступен — ещё «Попробовать бесплатно»."""
    screen = await _help(shop)
    assert _text(screen).startswith("Подписки пока нет.")
    assert _labels(screen) == ["Купить подписку", "Главное меню"]

    await put_setting(shop.session, TRIAL_ENABLED, True)
    await put_setting(shop.session, TRIAL_SQUADS, [UUID(int=1)])
    assert _labels(await _help(shop)) == [
        "Попробовать бесплатно",
        "Купить подписку",
        "Главное меню",
    ]


async def test_12_8_panel_outage_does_not_block_guide(shop: Shop) -> None:
    """4.12, 12.8: открытый простой панели не мешает показать сохранённую инструкцию."""
    subscription = await _subscription(shop)
    await _guide(shop, subscription)
    await add(shop.session, PanelOutage(started_at=datetime.now(UTC)))
    shop.panel.down = True
    assert isinstance(await _guide(shop, subscription), SendRichMessage)


async def test_12_7_stale_assigned_config_does_not_stop_refresh(shop: Shop) -> None:
    """12.7: конфиг, назначенный подписке, удалили в панели — обновление остальных
    конфигов (и конфига по умолчанию) всё равно проходит."""
    await _owner(shop)
    subscription = await _subscription(shop)
    subscription.subpage_config_uuid = UUID(SQUAD_CONFIG)
    await shop.session.flush()

    response = await shop.http.post("/api/admin/settings/panel/guide/refresh")

    assert response.status_code == 200
    assert response.json()["guide_refreshed_at"] is not None


# --- Решение 0060: инструкция на месте ---


async def test_0060_guide_from_card_opens_in_place_with_back(shop: Shop) -> None:
    """0060: «Как подключить» в карточке открывает инструкцию в том же сообщении, с
    «Назад» в карточку; «Главное меню» правит то же сообщение."""
    subscription = await _subscription(shop)
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    [guide] = [b for b in screen_buttons(shop.telegram.last()) if b.text == "Как подключить"]
    assert guide.callback_data is not None
    await shop.press(USER, guide.callback_data)

    screen = shop.telegram.last()
    assert isinstance(screen, EditMessageText)
    assert set(_platforms(screen)) == {"iOS", "Linux"}
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    data = {button.text: button.callback_data for row in markup.inline_keyboard for button in row}
    assert data["Назад"] == Card(subscription_id=subscription.id).pack()
    assert data["Главное меню"] == "menu:back"

    # «Другие приложения» сохраняет «Назад»
    await shop.press(
        USER,
        GuideApps(subscription_id=subscription.id, platform="ios", back=GuideBack.CARD).pack(),
    )
    assert "Назад" in _labels(shop.telegram.last())


async def test_0060_guide_from_event_is_new_message(shop: Shop) -> None:
    """0060: на событиях со ссылкой (С1, С3, С15) инструкция — новым сообщением, без
    «Назад»; «Главное меню» в ней правит то же сообщение."""
    subscription = await _subscription(shop)
    screen = await _guide(shop, subscription)
    assert isinstance(screen, SendRichMessage)
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    data = {button.text: button.callback_data for row in markup.inline_keyboard for button in row}
    assert "Назад" not in data
    assert data["Главное меню"] == "menu:back"


async def test_0060_guide_from_help_returns_to_help(shop: Shop) -> None:
    """0060: инструкция из /help — в том же сообщении, «Назад» возвращает /help."""
    subscription = await _subscription(shop)
    await _help(shop)
    await shop.press(USER, GuideHere(subscription_id=subscription.id, back=GuideBack.HELP).pack())
    assert isinstance(shop.telegram.last(), EditMessageText)
    await shop.press(USER, "help")
    back = shop.telegram.last()
    assert isinstance(back, EditMessageText)
    assert (back.text or "").startswith("Инструкция по подключению — по кнопке ниже.")
