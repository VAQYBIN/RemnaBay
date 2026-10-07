"""Подставная панель и окружение воркера для тестов сверки, вебхуков и сообщений."""

import json
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2

from remnabay import runtime
from remnabay.crypto import SecretBox
from remnabay.messaging import DeliveryError, OutgoingMessage
from remnabay.panel import PanelClient
from remnabay.payments import NotReadyApplier, PaymentApplier, Providers
from tests.conftest import REQUIRED_ENV

PANEL_URL = "https://panel.example.com"
_USER_PATH = re.compile(r"^/api/users/(\d+)$")
_USERNAME_PATH = re.compile(r"^/api/users/by-username/([\w-]+)$")
_RESET_PATH = re.compile(r"^/api/users/(\d+)/actions/reset-traffic$")
# Поля пользователя, которые меняет запрос PATCH /api/users
_UPDATABLE = (
    "expireAt",
    "trafficLimitBytes",
    "trafficLimitStrategy",
    "hwidDeviceLimit",
    "telegramId",
    "description",
    "externalSquadUuid",
)


def user_json(**overrides: Any) -> dict[str, Any]:
    """Пользователь панели 3.4.4 в формате ответа API."""
    user: dict[str, Any] = {
        "id": 7,
        "shortUuid": "abc123",
        "username": "rb_7",
        "status": "ACTIVE",
        "trafficLimitBytes": 0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": "2026-11-05T12:00:00.000Z",
        "telegramId": 100500,
        "email": None,
        "description": None,
        "tag": None,
        "hwidDeviceLimit": 3,
        "externalSquadUuid": None,
        "subRevokedAt": None,
        "lastTrafficResetAt": None,
        "createdAt": "2026-10-05T12:00:00.000Z",
        "updatedAt": "2026-10-05T12:00:00.000Z",
        "subscriptionUrl": "https://sub.example.com/abc123",
        "activeInternalSquads": [
            {"uuid": "6f0b2c3e-1d2a-4b5c-8d9e-0f1a2b3c4d5e", "name": "Default"}
        ],
        "userTraffic": {
            "usedTrafficBytes": 1024,
            "lifetimeUsedTrafficBytes": 2048,
            "onlineAt": None,
            "firstConnectedAt": None,
            "lastConnectedNodeUuid": None,
        },
    }
    user.update(overrides)
    return user


@dataclass
class FakePanel:
    """Панель с пользователями в памяти: создаёт, читает, меняет пользователей и
    обнуляет трафик."""

    users: dict[int, dict[str, Any]] = field(default_factory=dict[int, dict[str, Any]])
    down: bool = False
    # Панель на связи, но отвечает этой ошибкой (например, 401 — неверный токен)
    error_status: int | None = None
    requests: list[str] = field(default_factory=list[str])
    version: str = "3.4.4"
    # Лимит устройств в панели (HWID): None — не настроен
    hwid_enabled: bool | None = True
    # Внутренние сквады панели: uuid → название
    squads: dict[str, str] = field(default_factory=dict[str, str])
    # Тела запросов, которые меняют пользователей: (метод и путь, тело)
    writes: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    next_id: int = 1000
    # Ответ на запрос пользователя «потерялся»: изменение применено, ответ — ошибка шлюза
    lose_next_write: bool = False

    def add_user(self, **overrides: Any) -> dict[str, Any]:
        user = user_json(**overrides)
        self.users[user["id"]] = user
        return user

    def _by_username(self, username: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["username"] == username), None)

    def _written(self, request: httpx2.Request, user: dict[str, Any]) -> httpx2.Response:
        if self.lose_next_write:
            self.lose_next_write = False
            return httpx2.Response(502, json={"message": "Bad Gateway"})
        return httpx2.Response(200, json={"response": user})

    def _create(self, request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.writes.append(("POST /api/users", body))
        if self._by_username(body["username"]) is not None:
            return httpx2.Response(400, json={"message": "exists", "errorCode": "A019"})
        self.next_id += 1
        squads = [{"uuid": uuid, "name": "Squad"} for uuid in body.get("activeInternalSquads", [])]
        user = self.add_user(
            id=self.next_id,
            shortUuid=f"short{self.next_id}",
            username=body["username"],
            expireAt=body["expireAt"],
            trafficLimitBytes=body.get("trafficLimitBytes", 0),
            trafficLimitStrategy=body.get("trafficLimitStrategy", "NO_RESET"),
            hwidDeviceLimit=body.get("hwidDeviceLimit"),
            telegramId=body.get("telegramId"),
            description=body.get("description"),
            subscriptionUrl=f"https://sub.example.com/short{self.next_id}",
            activeInternalSquads=squads,
            createdAt=datetime.now(UTC).isoformat(),
            userTraffic={**user_json()["userTraffic"], "usedTrafficBytes": 0},
        )
        return self._written(request, user)

    def _update(self, request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.writes.append(("PATCH /api/users", body))
        user = self.users.get(body["id"])
        if user is None:
            return httpx2.Response(404, json={"message": "User not found", "errorCode": "A063"})
        for name in _UPDATABLE:
            if name in body:
                user[name] = body[name]
        if "activeInternalSquads" in body:
            user["activeInternalSquads"] = [
                {"uuid": uuid, "name": "Squad"} for uuid in body["activeInternalSquads"]
            ]
        return self._written(request, user)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.down:
            raise httpx2.ConnectError("панель недоступна", request=request)
        if self.error_status is not None:
            return httpx2.Response(self.error_status, json={"message": "ошибка"})
        if request.url.path == "/api/system/metadata":
            return httpx2.Response(200, json={"response": {"version": self.version}})
        if request.url.path == "/api/subscription-settings":
            hwid = None if self.hwid_enabled is None else {"enabled": self.hwid_enabled}
            return httpx2.Response(200, json={"response": {"hwidSettings": hwid}})
        if request.url.path == "/api/internal-squads":
            squads = [{"uuid": uuid, "name": name} for uuid, name in self.squads.items()]
            body = {"total": len(squads), "internalSquads": squads}
            return httpx2.Response(200, json={"response": body})
        if request.url.path == "/api/users" and request.method == "POST":
            return self._create(request)
        if request.url.path == "/api/users" and request.method == "PATCH":
            return self._update(request)
        reset = _RESET_PATH.match(request.url.path)
        if reset and request.method == "POST":
            user = self.users[int(reset.group(1))]
            self.writes.append((request.url.path, {}))
            user["userTraffic"] = {**user["userTraffic"], "usedTrafficBytes": 0}
            return httpx2.Response(200, json={"response": user})
        by_name = _USERNAME_PATH.match(request.url.path)
        if request.method == "GET" and by_name:
            found = self._by_username(by_name.group(1))
            if found is None:
                body = {"message": "User not found", "errorCode": "A063"}
                return httpx2.Response(404, json=body)
            return httpx2.Response(200, json={"response": found})
        match = _USER_PATH.match(request.url.path)
        if request.method == "GET" and match:
            user = self.users.get(int(match.group(1)))
            if user is None:
                body = {"message": "User not found", "errorCode": "A063"}
                return httpx2.Response(404, json=body)
            return httpx2.Response(200, json={"response": user})
        return httpx2.Response(404, json={"message": "Not found"})

    def client(self) -> PanelClient:
        return PanelClient(PANEL_URL, "token", transport=httpx2.MockTransport(self.handle))


@dataclass
class FakeSender:
    """Отправитель для тестов: запоминает сообщения, отвечает заготовленными ошибками."""

    errors: list[DeliveryError] = field(default_factory=list[DeliveryError])
    sent: list[tuple[int, OutgoingMessage]] = field(
        default_factory=list[tuple[int, OutgoingMessage]]
    )

    async def send(self, chat_id: int, message: OutgoingMessage) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, message))


@contextmanager
def fake_runtime(
    sender: FakeSender | None = None,
    panel: FakePanel | None = None,
    payments: PaymentApplier | None = None,
    providers: Providers | None = None,
) -> Generator[runtime.Runtime]:
    """Окружение воркера с подставными отправителем, панелью, шагом применения платежа
    и провайдерами."""
    with runtime.use(
        runtime.Runtime(
            sender=sender or FakeSender(),
            panel=(panel or FakePanel()).client(),
            payments=payments or NotReadyApplier(),
            providers=providers or Providers(SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"]), []),
        )
    ) as current:
        yield current
