"""Поддельный Telegram OpenID Connect: свой RSA-ключ, JWKS и подписанные ID token."""

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from remnabay.access import TelegramOidc
from remnabay.access._oidc import ISSUER, JWKS_URL, TOKEN_URL
from tests.conftest import REQUIRED_ENV

BOT_ID = "123456"
CLIENT_SECRET = REQUIRED_ENV["TELEGRAM_LOGIN_CLIENT_SECRET"]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwks() -> dict[str, Any]:
    numbers = KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "alg": "RS256",
                "n": _int_b64(numbers.n),
                "e": _int_b64(numbers.e),
            }
        ]
    }


def sign(claims: dict[str, Any], *, header: dict[str, Any] | None = None) -> str:
    head = header or {"alg": "RS256", "typ": "JWT", "kid": KID}
    signed = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = KEY.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signed}.{_b64(signature)}"


def claims_for(telegram_id: int | str, nonce: str, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": ISSUER,
        "aud": BOT_ID,
        "sub": "4909203185087370736",
        "iat": now,
        "exp": now + 30,
        "nonce": nonce,
        "id": str(telegram_id),
        "name": "Иван",
    }
    values.update(overrides)
    return values


@dataclass
class FakeTelegramOidc:
    """Сервер Telegram: по коду выдаёт ID token для заданного аккаунта.

    `token_for` получает nonce из запроса авторизации и возвращает ID token
    (или ответ целиком через `response`).
    """

    telegram_id: int = 500
    nonces: dict[str, str] = field(default_factory=dict[str, str])
    token_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    header: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    # Подпись испорчена: токен подделан
    tamper: bool = False

    def remember(self, code: str, nonce: str) -> None:
        self.nonces[code] = nonce

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == JWKS_URL:
            return httpx2.Response(200, json=jwks())
        if url == TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            form["authorization"] = request.headers.get("authorization", "")
            self.token_requests.append(form)
            if self.response is not None:
                return httpx2.Response(200, json=self.response)
            nonce = self.nonces.get(form.get("code", ""), "")
            claims = {**claims_for(self.telegram_id, nonce), **self.overrides}
            token = sign(claims, header=self.header)
            if self.tamper:
                head, payload, signature = token.split(".")
                token = f"{head}.{payload}.{signature[:-6]}AAAAAA"
            return httpx2.Response(
                200, json={"id_token": token, "token_type": "Bearer", "expires_in": 30}
            )
        return httpx2.Response(404)

    def client(self) -> TelegramOidc:
        return TelegramOidc(BOT_ID, CLIENT_SECRET, transport=httpx2.MockTransport(self.handle))
