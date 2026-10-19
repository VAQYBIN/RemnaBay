"""Бот после миграции: первое открытие усыновлённым клиентом, ссылки старого бота
(сценарий «Первое открытие бота усыновлённым клиентом», 9.13, 3.15)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import EditMessageText, SendRichMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._common import Card
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.shop import SHOP_STATE, ShopState
from tests.bot_support import telegram_user
from tests.domain_support import add, make_client, make_subscription, put_setting
from tests.web_support import Shop, running_shop

USER = telegram_user(777)
NEWCOMER = telegram_user(778)


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        yield shop


async def _adopted(shop: Shop) -> Client:
    client = await make_client(shop.session, telegram_id=USER.id)
    client.adopted_at = datetime.now(UTC)
    subscription = make_subscription(client, None, panel_user_id=1)
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = datetime.now(UTC) + timedelta(days=10)
    await add(shop.session, subscription)
    return client


def _text(shop: Shop) -> str:
    screen = shop.telegram.last()
    assert not isinstance(screen, SendRichMessage)
    assert screen.text is not None
    return screen.text


async def test_adopted_client_first_open_sees_welcome_back(shop: Shop) -> None:
    """Сценарий «Первое открытие бота усыновлённым клиентом», 9.4: при первом открытии —
    приветствие для существующего клиента и его подписки; дальше — обычное меню."""
    await _adopted(shop)

    await shop.send_text(USER, "/start")
    first = _text(shop)
    await shop.send_text(USER, "/start")
    second = _text(shop)

    assert first.startswith("С возвращением!")
    assert "Основная" in first
    assert not second.startswith("С возвращением!")


async def test_9_13_old_bot_referral_link_for_new_client(shop: Shop) -> None:
    """9.13, 3.15: ссылка старого бота распознаётся по перенесённому коду — новый клиент
    получает реферальную связь."""
    inviter = await _adopted(shop)
    await add(
        shop.session, ReferralCode(code="ref_uAB12CD34E", client_id=inviter.id, migrated=True)
    )

    await shop.send_text(NEWCOMER, "/start ref_uAB12CD34E")

    invited = await shop.session.scalar(
        select(TelegramAccount.client_id).where(TelegramAccount.telegram_id == NEWCOMER.id)
    )
    link = await shop.session.get_one(ReferralLink, invited)
    assert link.inviter_id == inviter.id


async def test_9_13_adopted_client_by_old_link_gets_no_link(shop: Shop) -> None:
    """3.15, сценарий: усыновлённый клиент, пришедший по ссылке старого бота, связи не
    получает — он уже существовал."""
    other = await make_client(shop.session, telegram_id=900)
    await add(shop.session, ReferralCode(code="ref_uZZ", client_id=other.id, migrated=True))
    client = await _adopted(shop)

    await shop.send_text(USER, "/start ref_uZZ")

    assert await shop.session.get(ReferralLink, client.id) is None


async def test_9_4_adopted_subscription_without_end_in_sight_is_shown(shop: Shop) -> None:
    """9.4, 0044: у бессрочного пользователя панели срок — 2099 год, позже предела
    Telegram для date_time: меню и карточка показывают дату текстом, а не падают."""
    client = await _adopted(shop)
    subscription = await shop.session.scalar(
        select(Subscription).where(Subscription.client_id == client.id)
    )
    assert subscription is not None
    subscription.expires_at = datetime(2099, 10, 4, 18, 35, tzinfo=UTC)
    await shop.session.commit()

    await shop.send_text(USER, "/start")
    menu = shop.telegram.last()
    assert not isinstance(menu, SendRichMessage)
    assert menu.text is not None
    assert "2099" in menu.text
    assert not [entity for entity in menu.entities or [] if entity.type == "date_time"]

    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    card = shop.telegram.last()
    assert isinstance(card, EditMessageText)
    assert card.rich_message is not None
    content = card.rich_message.model_dump_json(exclude_none=True)
    assert "2099" in content
    assert "unix_time" not in content
