"""Вход через Telegram OpenID Connect (1.4, решение 0053, research/telegram-oidc.md).

Authorization Code с PKCE (S256): браузер уходит на страницу Telegram, участник
подтверждает вход в приложении, Telegram возвращает код на адрес магазина, сервер
обменивает его на ID token с Client Secret и проверяет токен сам:
- подпись — по ключам Telegram (RS256 или ES256);
- издатель — https://oauth.telegram.org;
- получатель — ID бота магазина;
- срок действия и `nonce`.

Telegram ID участника — в claim `id` (строкой), а не в `sub`: `sub` — другой номер.
ID token живёт 30 секунд, поэтому код обменивается сразу.
"""

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import BaseModel, ConfigDict, ValidationError

from remnabay.crypto import SecretBox, SecretDecryptionError

ISSUER = "https://oauth.telegram.org"
AUTHORIZE_URL = f"{ISSUER}/auth"
TOKEN_URL = f"{ISSUER}/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
# `profile` нужен ради claim `id` — Telegram ID участника
SCOPE = "openid profile"
ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
# Допуск расхождения часов: токен живёт всего 30 секунд
CLOCK_SKEW = timedelta(seconds=60)
JWKS_TTL = timedelta(hours=1)
# Неизвестный ключ — перечитать ключи, но не чаще раза в минуту
JWKS_REFRESH_INTERVAL = timedelta(minutes=1)
REQUEST_TIMEOUT = timedelta(seconds=10)

logger = logging.getLogger(__name__)


class OidcError(Exception):
    """Telegram не подтвердил вход: код не обменялся или токен не прошёл проверку."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _int(text: str) -> int:
    return int.from_bytes(_b64url_decode(text), "big")


@dataclass(frozen=True)
class OidcAttempt:
    """Попытка входа: живёт в зашифрованной cookie браузера, который начал вход."""

    state: str
    nonce: str
    verifier: str

    @classmethod
    def new(cls) -> OidcAttempt:
        return cls(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            # RFC 7636: от 43 до 128 символов
            verifier=secrets.token_urlsafe(64),
        )

    @property
    def challenge(self) -> str:
        return _b64url(hashlib.sha256(self.verifier.encode()).digest())

    def seal(self, box: SecretBox) -> str:
        payload = {"state": self.state, "nonce": self.nonce, "verifier": self.verifier}
        return box.encrypt(json.dumps(payload))

    @classmethod
    def unseal(cls, box: SecretBox, value: str, *, max_age: timedelta) -> OidcAttempt | None:
        """Попытка из cookie; подделанная или старше срока подтверждения входа — `None`."""
        try:
            payload = json.loads(box.decrypt(value, max_age=max_age))
            return cls(state=payload["state"], nonce=payload["nonce"], verifier=payload["verifier"])
        except SecretDecryptionError, ValueError, KeyError, TypeError:
            return None


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id_token: str | None = None
    error: str | None = None


class _IdToken(BaseModel):
    """Нужные магазину claims ID token. `id` приходит строкой — разбирается в число."""

    model_config = ConfigDict(extra="ignore")

    iss: str
    aud: str | int
    exp: int
    iat: int
    nonce: str | None = None
    id: int | None = None


class TelegramOidc:
    """Клиент OpenID Connect Telegram одного бота: Client ID — ID бота (0053)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._http = httpx2.AsyncClient(
            timeout=REQUEST_TIMEOUT.total_seconds(), transport=transport
        )
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_at: datetime | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, attempt: OidcAttempt, redirect_uri: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": attempt.challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def telegram_id(
        self, code: str, attempt: OidcAttempt, redirect_uri: str, *, now: datetime
    ) -> int:
        """Telegram ID участника по коду с адреса возврата."""
        id_token = await self._exchange(code, attempt, redirect_uri)
        return await self._verify(id_token, attempt.nonce, now)

    async def _exchange(self, code: str, attempt: OidcAttempt, redirect_uri: str) -> str:
        try:
            response = await self._http.post(
                TOKEN_URL,
                auth=(self.client_id, self._secret),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": attempt.verifier,
                },
            )
            body = _TokenResponse.model_validate_json(response.content)
        except (httpx2.HTTPError, ValidationError) as error:
            raise OidcError(f"обмен кода не удался: {type(error).__name__}") from error
        # Ошибку Telegram отдаёт и с кодом 200 — в теле ответа
        if body.error or not body.id_token:
            raise OidcError(f"обмен кода отклонён: {body.error or 'нет id_token'}")
        return body.id_token

    async def _key(self, kid: str, now: datetime) -> dict[str, Any] | None:
        stale = self._keys_at is None or now - self._keys_at > JWKS_TTL
        recent = self._keys_at is not None and now - self._keys_at < JWKS_REFRESH_INTERVAL
        if stale or (kid not in self._keys and not recent):
            try:
                response = await self._http.get(JWKS_URL)
                keys = response.json()["keys"]
            except (httpx2.HTTPError, ValueError, KeyError) as error:
                raise OidcError("ключи Telegram недоступны") from error
            self._keys = {key["kid"]: key for key in keys if "kid" in key}
            self._keys_at = now
        return self._keys.get(kid)

    async def _verify(self, token: str, nonce: str, now: datetime) -> int:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
            header = json.loads(_b64url_decode(header_b64))
            claims = _IdToken.model_validate_json(_b64url_decode(payload_b64))
            signature = _b64url_decode(signature_b64)
        except (ValueError, ValidationError) as error:
            raise OidcError("ID token не разбирается") from error
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise OidcError(f"алгоритм подписи {algorithm} не принимается")
        key = await self._key(str(header.get("kid")), now)
        if key is None or key.get("alg", algorithm) != algorithm:
            raise OidcError("ключ подписи не найден")
        _check_signature(algorithm, key, f"{header_b64}.{payload_b64}".encode(), signature)

        moment = now.timestamp()
        skew = CLOCK_SKEW.total_seconds()
        if claims.iss != ISSUER:
            raise OidcError("чужой издатель токена")
        if str(claims.aud) != self.client_id:
            raise OidcError("токен выписан не для бота магазина")
        if claims.exp + skew < moment or claims.iat - skew > moment:
            raise OidcError("токен просрочен")
        if claims.nonce != nonce:
            raise OidcError("nonce не совпал")
        if claims.id is None:
            raise OidcError("в токене нет Telegram ID (claim id)")
        return claims.id


def _check_signature(algorithm: str, key: dict[str, Any], signed: bytes, signature: bytes) -> None:
    try:
        if algorithm == "RS256":
            public = rsa.RSAPublicNumbers(_int(key["e"]), _int(key["n"])).public_key()
            public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            curve = ec.EllipticCurvePublicNumbers(_int(key["x"]), _int(key["y"]), ec.SECP256R1())
            half = len(signature) // 2
            der = encode_dss_signature(
                int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big")
            )
            curve.public_key().verify(der, signed, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, KeyError, ValueError) as error:
        raise OidcError("подпись токена неверна") from error
