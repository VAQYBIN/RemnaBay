"""Эксперимент: вход через Telegram OpenID Connect (docs/research/telegram-oidc.md).

Код эксперимента в проект не переносится (CLAUDE.md, «Исследовательские эксперименты»),
переносятся только выводы. Запуск — на месте веба (порт 8000), туда ведёт туннель:

    docker compose -f compose.dev.yaml stop web
    uv run python spikes/telegram_oidc/spike.py
    # открыть https://<PUBLIC_URL>/spike/oidc — войти — посмотреть вывод
    docker compose -f compose.dev.yaml up -d web

Читает из .env: BOT_TOKEN (client_id — ID бота), OWNER_TELEGRAM_ID, PUBLIC_URL и
TELEGRAM_LOGIN_CLIENT_SECRET (Client Secret из @BotFather → Login Widget).
Результаты — в results.json рядом (не в git: там данные Telegram-аккаунта).
"""

import base64
import hashlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).with_name("results.json")
DISCOVERY = "https://oauth.telegram.org/.well-known/openid-configuration"
CALLBACK_PATHS = ("/spike/oidc/callback", "/api/admin/auth/telegram/callback")
PORT = 8000


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


ENV = read_env()
CLIENT_ID = ENV["BOT_TOKEN"].split(":", 1)[0]
CLIENT_SECRET = ENV.get("TELEGRAM_LOGIN_CLIENT_SECRET", "")
OWNER_ID = int(ENV["OWNER_TELEGRAM_ID"])
PUBLIC_URL = ENV["PUBLIC_URL"].rstrip("/")
METADATA: dict[str, Any] = httpx2.get(DISCOVERY, timeout=10).json()
# Попытки входа: state → (nonce, code_verifier, redirect_uri, время)
PENDING: dict[str, tuple[str, str, str, float]] = {}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def int_from(text: str) -> int:
    return int.from_bytes(b64url_decode(text), "big")


def verify_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Заголовок, claims и итог проверки подписи по JWKS Telegram."""
    header_b64, payload_b64, signature_b64 = token.split(".")
    header = json.loads(b64url_decode(header_b64))
    claims = json.loads(b64url_decode(payload_b64))
    jwks = httpx2.get(METADATA["jwks_uri"], timeout=10).json()["keys"]
    key = next((k for k in jwks if k.get("kid") == header.get("kid")), None)
    if key is None:
        return header, claims, f"нет ключа kid={header.get('kid')}"
    signed = f"{header_b64}.{payload_b64}".encode()
    signature = b64url_decode(signature_b64)
    try:
        match header.get("alg"):
            case "RS256":
                public = rsa.RSAPublicNumbers(int_from(key["e"]), int_from(key["n"])).public_key()
                public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
            case "ES256":
                public_ec = ec.EllipticCurvePublicNumbers(
                    int_from(key["x"]), int_from(key["y"]), ec.SECP256R1()
                ).public_key()
                half = len(signature) // 2
                der = encode_dss_signature(
                    int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big")
                )
                public_ec.verify(der, signed, ec.ECDSA(hashes.SHA256()))
            case other:
                return header, claims, f"алгоритм {other} в эксперименте не проверяется"
    except InvalidSignature:
        return header, claims, "ПОДПИСЬ НЕВЕРНА"
    return header, claims, "подпись верна"


def save(result: dict[str, Any]) -> None:
    previous = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else []
    previous.append(result)
    RESULTS.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/spike/oidc":
            self._send(
                200,
                "<p><a href='/spike/oidc/start'>Войти через Telegram (OIDC)</a></p>"
                "<p><a href='/spike/oidc/start?callback=1'>То же, возврат на путь магазина</a></p>",
            )
        elif url.path == "/spike/oidc/start":
            callback = CALLBACK_PATHS[1] if query.get("callback") else CALLBACK_PATHS[0]
            redirect_uri = PUBLIC_URL + callback
            state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            verifier = secrets.token_urlsafe(48)
            PENDING[state] = (nonce, verifier, redirect_uri, time.time())
            params = {
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
            self._send(
                302, "", {"Location": f"{METADATA['authorization_endpoint']}?{urlencode(params)}"}
            )
        elif url.path in CALLBACK_PATHS:
            self._callback(query)
        else:
            self._send(404, "не найдено")

    def _callback(self, query: dict[str, str]) -> None:
        result: dict[str, Any] = {"callback_query_keys": sorted(query), "error": query.get("error")}
        pending = PENDING.pop(query.get("state", ""), None)
        if "code" not in query or pending is None:
            result["note"] = "нет кода или неизвестный state"
            save(result)
            self._send(200, f"<pre>{json.dumps(result, ensure_ascii=False, indent=2)}</pre>")
            return
        nonce, verifier, redirect_uri, started = pending
        response = httpx2.post(
            METADATA["token_endpoint"],
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={
                "grant_type": "authorization_code",
                "code": query["code"],
                "redirect_uri": redirect_uri,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
            timeout=10,
        )
        body = response.json()
        result |= {
            "seconds_from_start": round(time.time() - started, 1),
            "token_status": response.status_code,
            "token_response_keys": sorted(body),
            "token_error": body.get("error"),
            "expires_in": body.get("expires_in"),
        }
        if "id_token" in body:
            header, claims, signature = verify_jwt(body["id_token"])
            user_id = claims.get("id")
            result |= {
                "jwt_header": header,
                "signature": signature,
                "claims_keys": sorted(claims),
                "iss": claims.get("iss"),
                "aud": claims.get("aud"),
                "aud_type": type(claims.get("aud")).__name__,
                "aud_matches_bot": str(claims.get("aud")) == CLIENT_ID,
                "lifetime_seconds": (claims.get("exp", 0) - claims.get("iat", 0)),
                "nonce_returned": "nonce" in claims,
                "nonce_matches": claims.get("nonce") == nonce,
                "sub_equals_id": str(claims.get("sub")) == str(user_id),
                "id_type": type(user_id).__name__,
                "id_matches_owner": user_id is not None and int(user_id) == OWNER_ID,
            }
        save(result)
        self._send(200, f"<pre>{json.dumps(result, ensure_ascii=False, indent=2)}</pre>")


if __name__ == "__main__":
    if not CLIENT_SECRET:
        raise SystemExit("В .env нет TELEGRAM_LOGIN_CLIENT_SECRET — возьмите его в @BotFather")
    print(f"client_id={CLIENT_ID}; откройте {PUBLIC_URL}/spike/oidc", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 — туннель на этот порт
