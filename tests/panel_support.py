"""Подставная панель и окружение воркера для тестов сверки, вебхуков и сообщений."""

import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2

from remnabay import runtime
from remnabay.messaging import DeliveryError, OutgoingMessage
from remnabay.panel import PanelClient
from remnabay.payments import NotReadyApplier, PaymentApplier

PANEL_URL = "https://panel.example.com"
_USER_PATH = re.compile(r"^/api/users/(\d+)$")


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
    """Панель с пользователями в памяти: отвечает на запрос пользователя по id."""

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
) -> Generator[runtime.Runtime]:
    """Окружение воркера с подставными отправителем, панелью и шагом применения платежа."""
    with runtime.use(
        runtime.Runtime(
            sender=sender or FakeSender(),
            panel=(panel or FakePanel()).client(),
            payments=payments or NotReadyApplier(),
        )
    ) as current:
        yield current
