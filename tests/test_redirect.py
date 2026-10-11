"""Страница-переходник (блок 12; 12.10–12.15; решения 0019, 0038, 0058)."""

import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._common import Guide
from remnabay.brand import BRAND_NAME, BRAND_PRIMARY_COLOR, build_palette
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.guide import DEFAULT_CONFIG_UUID, RedirectLink, fetch_config
from remnabay.shop import SHOP_STATE, ShopState
from tests.bot_support import telegram_user
from tests.domain_support import add, make_client, make_subscription, put_setting
from tests.guide_support import guide_config
from tests.panel_support import DEFAULT_CONFIG
from tests.web_support import Shop, running_shop

USER = telegram_user(777)


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        await put_setting(db_session, BRAND_NAME, "Енот VPN")
        await put_setting(db_session, BRAND_PRIMARY_COLOR, "#7c3aed")
        shop.panel.subpage_configs[DEFAULT_CONFIG] = guide_config()
        yield shop


async def _subscription(shop: Shop) -> Subscription:
    client = await make_client(shop.session, telegram_id=USER.id)
    subscription = make_subscription(client, None, panel_user_id=7)
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = datetime(2026, 12, 1, tzinfo=UTC)
    await add(shop.session, subscription)
    return subscription


async def _token(shop: Shop, subscription: Subscription, app: str = "Happ") -> str:
    """Токен кнопки импорта — из инструкции в боте."""
    await shop.press(USER, Guide(subscription_id=subscription.id).pack())
    token = await shop.session.scalar(
        select(RedirectLink.token).where(
            RedirectLink.subscription_id == subscription.id, RedirectLink.app_name == app
        )
    )
    assert token is not None
    return token


async def _page(shop: Shop, token: str) -> str:
    response = await shop.http.get(f"/connect/{token}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


async def test_12_10_12_12_page_opens_app_with_server_built_deeplink(shop: Shop) -> None:
    """12.10, 12.12: диплинк собран на сервере из конфига и ссылки подписки; страница
    открывает приложение сама, показывает «Открыть Happ» и ссылки на установку."""
    subscription = await _subscription(shop)
    page = await _page(shop, await _token(shop, subscription))
    deeplink = f"happ://add/{subscription.subscription_url}"
    assert f'window.location.href="{deeplink}"' in page
    assert f'href="{deeplink}"' in page
    assert "Открываем Happ…" in page
    assert "Открыть Happ" in page
    assert "Приложение не установлено?" in page
    assert 'href="https://apps.apple.com/app/happ"' in page
    assert "Открыть в App Store" in page


async def test_12_10_username_variable_in_template(shop: Shop) -> None:
    """12.10: шаблон с именем пользователя панели — подставляется на сервере."""
    subscription = await _subscription(shop)
    await shop.press(USER, Guide(subscription_id=subscription.id).pack())
    token = await shop.session.scalar(
        select(RedirectLink.token).where(RedirectLink.app_name == "Happ")
    )
    assert token is not None
    link = await shop.session.get(RedirectLink, token)
    assert link is not None
    link.app_name = "Stash"
    link.template = "stash://install-config?url={{SUBSCRIPTION_LINK}}&name={{USERNAME}}"
    await shop.session.flush()
    page = await _page(shop, token)
    assert f"name={subscription.panel_username}" in page


async def test_12_10_page_accepts_no_redirect_address(shop: Shop) -> None:
    """12.10: произвольный адрес перенаправления страница не принимает."""
    subscription = await _subscription(shop)
    token = await _token(shop, subscription)
    response = await shop.http.get(f"/connect/{token}?redirect_to=https://evil.example.com")
    assert "evil.example.com" not in response.text


async def test_12_11_subscription_link_not_in_page_address(shop: Shop) -> None:
    """12.11: ссылка на подписку не попадает в адрес страницы; страница не кэшируется и
    не передаёт адрес дальше."""
    subscription = await _subscription(shop)
    token = await _token(shop, subscription)
    assert subscription.panel_short_uuid not in token
    response = await shop.http.get(f"/connect/{token}")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_12_13_language_of_client_and_device_theme(shop: Shop) -> None:
    """12.13: страница на языке клиента и в теме устройства; тема неизвестна — тёмная."""
    subscription = await _subscription(shop)
    page = await _page(shop, await _token(shop, subscription))
    assert '<html lang="ru">' in page
    palette = build_palette("#7c3aed", None)
    dark, light = palette.dark.tokens["bg"], palette.light.tokens["bg"]
    root = re.search(r":root\{([^}]*)\}", page)
    assert root is not None
    assert f"--rb-bg:{dark};" in root.group(1)
    assert "@media (prefers-color-scheme: light){:root{" in page
    assert f"--rb-bg:{light};" in page.split("prefers-color-scheme: light", 1)[1]


async def test_12_15_only_operator_brand(shop: Shop) -> None:
    """12.15, 0038: страница оформлена брендом оператора — название и фирменный цвет;
    упоминания RemnaBay нет."""
    subscription = await _subscription(shop)
    page = await _page(shop, await _token(shop, subscription))
    assert "Енот VPN" in page
    assert build_palette("#7c3aed", None).dark.tokens["primary"] in page
    assert "remnabay" not in page.lower()


async def test_12_15_invalid_page_has_no_remnabay(shop: Shop) -> None:
    """12.14, 12.15: неизвестный токен — `redirect.invalid`, оформление тоже бренда."""
    page = await _page(shop, "unknown-token")
    assert "Ссылка устарела. Вернитесь в бот и откройте инструкцию заново." in page
    assert "Енот VPN" in page
    assert "remnabay" not in page.lower()
    assert "window.location" not in page


@pytest.mark.parametrize("case", ["deleted", "revoked", "app_removed"])
async def test_12_14_token_invalid_after_change(shop: Shop, case: str) -> None:
    """12.14, 0058: токен бессрочный, но гаснет, если подписку удалили, ссылку подписки
    обновили (4.29) или приложения больше нет в конфиге."""
    subscription = await _subscription(shop)
    token = await _token(shop, subscription)
    assert "Открыть Happ" in await _page(shop, token)
    match case:
        case "deleted":
            subscription.deleted_at = datetime.now(UTC)
        case "revoked":
            subscription.panel_short_uuid = "new-short"
            subscription.subscription_url = "https://sub.example.com/new-short"
        case _:
            config = guide_config()
            config["platforms"]["ios"]["apps"] = config["platforms"]["ios"]["apps"][1:]
            shop.panel.subpage_configs[DEFAULT_CONFIG] = config
            await fetch_config(shop.session, shop.panel.client(), DEFAULT_CONFIG_UUID)
    await shop.session.flush()
    page = await _page(shop, token)
    assert "Ссылка устарела" in page
    assert subscription.subscription_url not in page


async def test_12_14_token_lives_while_link_is_the_same(shop: Shop) -> None:
    """12.14: пока ссылка подписки не менялась, старая кнопка работает — токен один и
    тот же при каждом открытии инструкции."""
    subscription = await _subscription(shop)
    first = await _token(shop, subscription)
    second = await _token(shop, subscription)
    assert first == second
    assert "Открыть Happ" in await _page(shop, first)
