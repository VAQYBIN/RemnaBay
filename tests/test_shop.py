"""Магазин не открыт, открыт, временно закрыт: ответ бота (1.13, 1.21, 1.22)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand import BRAND_NAME
from remnabay.domain.team import TeamRole
from remnabay.shop import SHOP_STATE, SHOP_TESTERS, ShopState, sales_open
from tests.bot_support import BOT_NAME, bot_harness, telegram_user
from tests.domain_support import add, make_team_member, put_setting

UNKNOWN = "Не совсем понял. Откройте главное меню — там всё самое нужное."


def _coming_soon(brand: str) -> str:
    return f"{brand} скоро откроется. Загляните чуть позже — мы почти готовы."


async def test_1_13_not_opened_shop_answers_any_message(db_session: AsyncSession) -> None:
    """1.13: пока магазин не открыт, бот на любое сообщение отвечает «скоро откроется»."""
    await put_setting(db_session, BRAND_NAME, "Енот VPN")

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "/start")
        await harness.send_text(telegram_user(100), "купить")

    assert harness.telegram.texts() == [_coming_soon("Енот VPN")] * 2


async def test_1_13_not_opened_shop_answers_button_press(db_session: AsyncSession) -> None:
    """1.13: нажатие кнопки старого сообщения — тот же ответ."""
    async with bot_harness(db_session) as harness:
        await harness.press(telegram_user(100), "menu:buy")

    assert harness.telegram.texts() == [_coming_soon(BOT_NAME)]


async def test_1_13_brand_name_falls_back_to_bot_name(db_session: AsyncSession) -> None:
    """Пока название бренда не задано, в тексте — имя бота в Telegram, а не RemnaBay (0038)."""
    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "привет")

    assert harness.telegram.texts() == [_coming_soon(BOT_NAME)]
    assert "RemnaBay" not in harness.telegram.texts()[0]


@pytest.mark.parametrize("role", list(TeamRole))
async def test_1_13_team_uses_bot_as_usual(db_session: AsyncSession, role: TeamRole) -> None:
    """1.13: для участников команды бот работает как обычно."""
    await add(db_session, make_team_member(telegram_id=500, role=role))

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(500), "привет")

    assert harness.telegram.texts() == [UNKNOWN]


async def test_1_21_tester_uses_bot_as_usual(db_session: AsyncSession) -> None:
    """1.21: для тестировщиков из настроек бот работает как обычно, для остальных — нет."""
    await put_setting(db_session, SHOP_TESTERS, [600, 601])

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(601), "привет")
        await harness.send_text(telegram_user(700), "привет")

    assert harness.telegram.texts() == [UNKNOWN, _coming_soon(BOT_NAME)]


async def test_1_13_open_shop_answers_everyone(db_session: AsyncSession) -> None:
    """1.13: открытый магазин работает для всех клиентов."""
    await put_setting(db_session, SHOP_STATE, ShopState.OPEN)

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "привет")

    assert harness.telegram.texts() == [UNKNOWN]


async def test_1_22_paused_shop_keeps_bot_for_clients(db_session: AsyncSession) -> None:
    """1.22: временно закрытый магазин бот не прячет — клиенты видят свои подписки;
    покупки, продления и триалы закрыты (`sales_open`)."""
    await put_setting(db_session, SHOP_STATE, ShopState.PAUSED)

    async with bot_harness(db_session) as harness:
        await harness.send_text(telegram_user(100), "привет")

    assert harness.telegram.texts() == [UNKNOWN]
    assert await sales_open(db_session) is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [(ShopState.NOT_OPENED, False), (ShopState.OPEN, True), (ShopState.PAUSED, False)],
)
async def test_1_22_sales_open_only_in_open_shop(
    db_session: AsyncSession, state: ShopState, expected: bool
) -> None:
    await put_setting(db_session, SHOP_STATE, state)

    assert await sales_open(db_session) is expected
