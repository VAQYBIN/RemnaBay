"""Вход через Telegram OpenID Connect (1.4, 1.5, 1.6, решение 0053)."""

import base64
import hashlib
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.access._oidc import AUTHORIZE_URL, ISSUER
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.web.admin import OIDC_COOKIE, SESSION_COOKIE
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_team_member
from tests.oidc_support import BOT_ID, CLIENT_SECRET, KID
from tests.web_support import Shop, running_shop

START = "/api/admin/auth/telegram/start"
CALLBACK = "/api/admin/auth/telegram/callback"
REDIRECT_URI = "https://shop.example.com/api/admin/auth/telegram/callback"
OWNER_TG = 500


@pytest.fixture
async def shop(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> AsyncGenerator[Shop]:
    del valid_env
    monkeypatch.setenv("BOT_TOKEN", f"{BOT_ID}:test-bot-token")
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = make_team_member(telegram_id=OWNER_TG, role=TeamRole.OWNER)
    await add(shop.session, member)
    return member


async def _start(shop: Shop) -> dict[str, str]:
    """«Войти через Telegram»: параметры перехода на страницу Telegram."""
    response = await shop.http.get(START)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE_URL + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _login(shop: Shop, code: str = "code-1", state: str | None = None) -> Any:
    """Участник подтвердил вход в Telegram — Telegram возвращает его с кодом."""
    params = await _start(shop)
    shop.oidc.remember(code, params["nonce"])
    return await shop.http.get(CALLBACK, params={"code": code, "state": state or params["state"]})


def _location(response: Any) -> str:
    location: str = response.headers["location"]
    return location


async def test_1_4_start_redirects_to_telegram_with_pkce(shop: Shop) -> None:
    """1.4: переход на страницу Telegram — код с PKCE S256, scope с профилем (claim id)."""
    params = await _start(shop)

    assert params["client_id"] == BOT_ID
    assert params["redirect_uri"] == REDIRECT_URI
    assert params["response_type"] == "code"
    assert params["scope"] == "openid profile"
    assert params["code_challenge_method"] == "S256"
    assert params["state"]
    assert params["nonce"]
    assert OIDC_COOKIE in shop.http.cookies


async def test_1_4_confirmed_login_opens_session(shop: Shop, owner: TeamMember) -> None:
    """1.4: подтверждение в Telegram — в браузере открывается сессия."""
    response = await _login(shop)

    assert response.status_code == 303
    assert _location(response) == "/admin/"
    assert SESSION_COOKIE in shop.http.cookies
    assert OIDC_COOKIE not in shop.http.cookies
    me = await shop.http.get("/api/admin/auth/me")
    assert me.json()["id"] == owner.id
    entries = await shop.session.scalars(
        select(JournalEntry).where(journaled_in_test(), JournalEntry.action == "team.login")
    )
    assert [e.details["method"] for e in entries] == ["oidc"]


async def test_0053_code_exchange_uses_secret_and_verifier(shop: Shop, owner: TeamMember) -> None:
    """0053: код меняется на токены с Client Secret и code_verifier, который подходит к
    code_challenge (RFC 7636)."""
    del owner
    params = await _start(shop)
    shop.oidc.remember("code-1", params["nonce"])

    await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})

    [request] = shop.oidc.token_requests
    expected = base64.b64encode(f"{BOT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request["authorization"] == f"Basic {expected}"
    assert request["grant_type"] == "authorization_code"
    assert request["redirect_uri"] == REDIRECT_URI
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(request["code_verifier"].encode()).digest()
    ).rstrip(b"=")
    assert challenge.decode() == params["code_challenge"]


async def test_1_6_non_team_account_is_rejected(shop: Shop) -> None:
    """1.6: аккаунт не из команды — отказ на странице входа и запись в журнал."""
    shop.oidc.telegram_id = 900

    response = await _login(shop)

    assert _location(response) == "/admin/login?error=rejected"
    assert SESSION_COOKIE not in shop.http.cookies
    rejected = await shop.session.scalars(
        select(JournalEntry).where(
            journaled_in_test(), JournalEntry.action == "team.login_rejected"
        )
    )
    assert [(e.details["telegram_id"], e.details["method"]) for e in rejected] == [(900, "oidc")]


async def test_0053_numeric_id_claim_is_accepted(shop: Shop, owner: TeamMember) -> None:
    """Telegram присылает id строкой; число тоже принимается."""
    del owner
    shop.oidc.overrides = {"id": OWNER_TG}

    assert _location(await _login(shop)) == "/admin/"


async def test_1_5_attempt_is_single_use(shop: Shop, owner: TeamMember) -> None:
    """1.5: попытка входа одноразовая — повтор того же кода без сессии отклоняется."""
    del owner
    params = await _start(shop)
    shop.oidc.remember("code-1", params["nonce"])
    first = await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})
    shop.http.cookies.clear()

    again = await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})

    assert _location(first) == "/admin/"
    assert _location(again) == "/admin/login?error=expired"


async def test_0053_repeated_callback_after_login_goes_to_admin(
    shop: Shop, owner: TeamMember
) -> None:
    """Браузер открыл адрес возврата ещё раз после входа — в админку, а не ошибка."""
    del owner
    params = await _start(shop)
    shop.oidc.remember("code-1", params["nonce"])
    await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})

    again = await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})

    assert _location(again) == "/admin/"
    assert len(shop.oidc.token_requests) == 1


async def test_1_5_attempt_expires(
    shop: Shop, owner: TeamMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.5: попытка действует срок подтверждения входа (5 минут)."""
    del owner
    params = await _start(shop)
    shop.oidc.remember("code-1", params["nonce"])
    later = time.time() + 6 * 60
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: later)

    response = await shop.http.get(CALLBACK, params={"code": "code-1", "state": params["state"]})

    assert _location(response) == "/admin/login?error=expired"
    assert shop.oidc.token_requests == []


@pytest.mark.parametrize("cookie", [True, False])
async def test_0053_foreign_state_is_rejected(shop: Shop, owner: TeamMember, cookie: bool) -> None:
    """Чужой state или вход без попытки этого браузера — отказ (защита от подмены входа)."""
    del owner
    await _start(shop)
    if not cookie:
        shop.http.cookies.clear()

    response = await shop.http.get(CALLBACK, params={"code": "code-1", "state": "чужой"})

    assert _location(response) == "/admin/login?error=expired"
    assert shop.oidc.token_requests == []


async def test_0053_refusal_without_code(shop: Shop) -> None:
    """Без кода (например, error от Telegram) сессии нет."""
    params = await _start(shop)

    response = await shop.http.get(
        CALLBACK, params={"error": "access_denied", "state": params["state"]}
    )

    assert _location(response) == "/admin/login?error=expired"


@pytest.mark.parametrize(
    ("overrides", "header", "response_body"),
    [
        ({"aud": "999"}, None, None),
        ({"iss": "https://evil.example"}, None, None),
        ({"exp": int(time.time()) - 600, "iat": int(time.time()) - 630}, None, None),
        ({"iat": int(time.time()) + 600, "exp": int(time.time()) + 630}, None, None),
        ({"nonce": "чужой"}, None, None),
        ({"id": None}, None, None),
        ({}, {"alg": "none", "typ": "JWT", "kid": KID}, None),
        ({}, {"alg": "HS256", "typ": "JWT", "kid": KID}, None),
        ({}, {"alg": "RS256", "typ": "JWT", "kid": "unknown"}, None),
        ({}, None, {"error": "invalid_grant"}),
    ],
    ids=[
        "чужой бот",
        "чужой издатель",
        "просрочен",
        "из будущего",
        "чужой nonce",
        "нет id",
        "alg none",
        "alg HS256",
        "неизвестный ключ",
        "ошибка обмена",
    ],
)
async def test_0053_untrusted_token_is_rejected(
    shop: Shop,
    owner: TeamMember,
    overrides: dict[str, Any],
    header: dict[str, Any] | None,
    response_body: dict[str, Any] | None,
) -> None:
    """0053: токен проверяется сам — подпись, издатель, получатель, срок, nonce, id."""
    del owner
    shop.oidc.overrides = overrides
    shop.oidc.header = header
    shop.oidc.response = response_body

    response = await _login(shop)

    assert _location(response) == "/admin/login?error=failed"
    assert SESSION_COOKIE not in shop.http.cookies


async def test_0053_tampered_signature_is_rejected(shop: Shop, owner: TeamMember) -> None:
    """0053: подделанная подпись ID token — отказ."""
    del owner
    shop.oidc.tamper = True

    response = await _login(shop)

    assert _location(response) == "/admin/login?error=failed"


def test_0053_issuer_is_telegram() -> None:
    assert ISSUER == "https://oauth.telegram.org"
