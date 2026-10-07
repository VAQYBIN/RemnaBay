"""Вход в админку через Telegram (1.4, 1.5, 1.6, решение 0051)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access import (
    LoginClosed,
    open_bot_login,
    start_bot_login,
)
from remnabay.domain.team import LoginRequest, TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.shop import SHOP_STATE, ShopState
from remnabay.web.admin import LOGIN_PAGE_PATH, SESSION_COOKIE
from tests.bot_support import BOT_NAME, BOT_USERNAME, telegram_user
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_team_member, put_setting
from tests.web_support import Shop, running_shop

OWNER_TG = 500
STRANGER_TG = 900
UNKNOWN = "Не совсем понял. Откройте главное меню — там всё самое нужное."
CONFIRMED = "Вход подтверждён — вернитесь в браузер."
EXPIRED = "Подтверждение устарело. Начните вход заново на странице админки."
COMING_SOON_TAIL = "Загляните чуть позже — мы почти готовы."


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(db_session: AsyncSession) -> TeamMember:
    member = make_team_member(telegram_id=OWNER_TG, role=TeamRole.OWNER)
    await add(db_session, member)
    return member


async def _request_login(shop: Shop) -> tuple[str, str]:
    """Кнопка «Войти через Telegram»: ссылка на бот и код."""
    response = await shop.http.post("/api/admin/auth/login-requests")
    assert response.status_code == 200
    body = response.json()
    return body["bot_link"], body["code"]


def _start_command(bot_link: str) -> str:
    start = parse_qs(urlsplit(bot_link).query)["start"][0]
    return f"/start {start}"


def _confirm_button(shop: Shop) -> str:
    message = shop.telegram.sent()[-1]
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    [[button]] = message.reply_markup.inline_keyboard
    assert button.callback_data is not None
    return button.callback_data


def _edits(shop: Shop) -> list[str]:
    return [r.text or "" for r in shop.telegram.requests if isinstance(r, EditMessageText)]


async def _poll(shop: Shop) -> str:
    response = await shop.http.post("/api/admin/auth/login-requests/poll")
    assert response.status_code == 200
    status: str = response.json()["status"]
    return status


async def test_1_4_web_login_confirmed_in_bot(shop: Shop, owner: TeamMember) -> None:
    """1.4: «Войти через Telegram» → подтверждение в боте → сессия в браузере."""
    bot_link, code = await _request_login(shop)
    assert bot_link.startswith(f"https://t.me/{BOT_USERNAME}?start=login_")
    assert len(code) == 4

    assert await _poll(shop) == "pending"
    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))
    confirm = shop.telegram.sent()[-1]
    assert confirm.text == (
        f"Вход в админку {BOT_NAME}. Код: {code}. Подтвердите, только если видите этот же "
        "код на странице входа. Если вы не входили — ничего не нажимайте."
    )
    await shop.press(telegram_user(OWNER_TG), _confirm_button(shop))

    assert _edits(shop) == [CONFIRMED]
    assert await _poll(shop) == "signed_in"
    me = await shop.http.get("/api/admin/auth/me")
    assert me.status_code == 200
    assert me.json() == {"id": owner.id, "telegram_id": OWNER_TG, "name": None, "role": "owner"}
    journaled = await shop.session.scalars(
        select(JournalEntry.action).where(journaled_in_test(), JournalEntry.action == "team.login")
    )
    assert list(journaled) == ["team.login"]


async def test_1_4_login_works_while_shop_not_opened(shop: Shop, owner: TeamMember) -> None:
    """1.4, 1.13: вход нужен и до открытия магазина — участник команды проходит."""
    del owner
    await put_setting(shop.session, SHOP_STATE, ShopState.NOT_OPENED)
    bot_link, _ = await _request_login(shop)

    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))
    await shop.press(telegram_user(OWNER_TG), _confirm_button(shop))

    assert await _poll(shop) == "signed_in"


async def test_1_4_admin_command_for_team(shop: Shop, owner: TeamMember) -> None:
    """1.4 (0053): на /admin участник команды получает кнопку со ссылкой на страницу входа."""
    del owner
    await shop.send_text(telegram_user(OWNER_TG), "/admin")

    [message] = shop.telegram.sent()
    assert message.text == f"Вход в админку {BOT_NAME} — по кнопке ниже."
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    [[button]] = message.reply_markup.inline_keyboard
    assert button.text == "Войти в админку"
    assert button.url == f"https://shop.example.com{LOGIN_PAGE_PATH}"
    assert button.login_url is None


@pytest.mark.parametrize(
    ("state", "answer"),
    [
        (ShopState.OPEN, UNKNOWN),
        (
            ShopState.NOT_OPENED,
            f"{BOT_NAME} скоро откроется. Загляните чуть позже — мы почти готовы.",
        ),
    ],
)
async def test_1_4_admin_command_for_others_is_unknown(
    shop: Shop, state: ShopState, answer: str
) -> None:
    """1.4: остальным на /admin бот отвечает как на непонятное сообщение."""
    await put_setting(shop.session, SHOP_STATE, state)

    await shop.send_text(telegram_user(STRANGER_TG), "/admin")

    assert shop.telegram.texts() == [answer]


async def test_1_5_confirmation_single_use(shop: Shop, owner: TeamMember) -> None:
    """1.5: подтверждённый вход выдаёт сессию один раз; повторное нажатие — устарело."""
    del owner
    bot_link, _ = await _request_login(shop)
    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))
    button = _confirm_button(shop)
    await shop.press(telegram_user(OWNER_TG), button)
    assert await _poll(shop) == "signed_in"

    await shop.press(telegram_user(OWNER_TG), button)
    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))

    assert _edits(shop) == [CONFIRMED, EXPIRED]
    assert shop.telegram.texts()[-1] == EXPIRED
    assert await _poll(shop) == "expired"


async def test_1_5_confirmation_expires(shop: Shop, owner: TeamMember) -> None:
    """1.5: подтверждение действует ограниченное время."""
    del owner
    bot_link, _ = await _request_login(shop)
    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))
    await shop.session.execute(
        update(LoginRequest).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    await shop.press(telegram_user(OWNER_TG), _confirm_button(shop))

    assert _edits(shop) == [EXPIRED]
    assert await _poll(shop) == "expired"


async def test_1_5_link_expires_after_ttl(db_session: AsyncSession) -> None:
    """1.5: ссылка входа после 5 минут не открывается."""
    await add(db_session, make_team_member(telegram_id=OWNER_TG))
    start = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    new = await start_bot_login(db_session, now=start)

    late = await open_bot_login(
        db_session, new.start_parameter, OWNER_TG, now=start + timedelta(minutes=5, seconds=1)
    )

    assert late == LoginClosed.EXPIRED


@pytest.mark.parametrize("state", [ShopState.OPEN, ShopState.NOT_OPENED])
async def test_1_6_non_team_rejected(shop: Shop, state: ShopState) -> None:
    """1.6: аккаунт не из команды — вход отклонён: в боте — как непонятное сообщение,
    на странице — отказ, в журнале — запись."""
    await put_setting(shop.session, SHOP_STATE, state)
    bot_link, _ = await _request_login(shop)

    await shop.send_text(telegram_user(STRANGER_TG), _start_command(bot_link))

    assert shop.telegram.texts() == [UNKNOWN]
    assert await _poll(shop) == "rejected"
    assert (await shop.http.get("/api/admin/auth/me")).status_code == 401
    rejected = await shop.session.scalars(
        select(JournalEntry).where(
            journaled_in_test(), JournalEntry.action == "team.login_rejected"
        )
    )
    assert [e.details["telegram_id"] for e in rejected] == [STRANGER_TG]


async def test_1_6_forwarded_confirmation_does_not_sign_in(shop: Shop, owner: TeamMember) -> None:
    """Подтвердить может только тот, кто открыл ссылку: чужое нажатие ничего не даёт."""
    del owner
    other_owner = make_team_member(telegram_id=501, role=TeamRole.OWNER)
    await add(shop.session, other_owner)
    bot_link, _ = await _request_login(shop)
    await shop.send_text(telegram_user(OWNER_TG), _start_command(bot_link))

    await shop.press(telegram_user(501), _confirm_button(shop))

    assert _edits(shop) == [EXPIRED]
    assert await _poll(shop) == "pending"


async def test_revoked_member_loses_session_at_once(shop: Shop, owner: TeamMember) -> None:
    """Отзыв доступа завершает сессию сразу (01-domain)."""
    await shop.sign_in(owner)
    assert (await shop.http.get("/api/admin/auth/me")).status_code == 200

    owner.revoked_at = datetime.now(UTC)
    await shop.session.flush()

    assert (await shop.http.get("/api/admin/auth/me")).status_code == 401


async def test_logout_ends_session(shop: Shop, owner: TeamMember) -> None:
    await shop.sign_in(owner)
    token = shop.http.cookies[SESSION_COOKIE]

    response = await shop.http.post("/api/admin/auth/logout")

    assert response.status_code == 204
    shop.http.cookies.set(SESSION_COOKIE, token)
    assert (await shop.http.get("/api/admin/auth/me")).status_code == 401


async def test_0051_foreign_origin_is_refused(shop: Shop) -> None:
    """0051: изменяющий запрос с чужого сайта отклоняется."""
    response = await shop.http.post(
        "/api/admin/auth/login-requests", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert not [r for r in shop.telegram.requests if isinstance(r, SendMessage)]
