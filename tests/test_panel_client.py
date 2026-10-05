"""Клиент панели на подставном транспорте httpx2 (решение 0042; 4.1, 4.30)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from remnabay.panel import (
    CreateUser,
    DeviceEvent,
    DeviceEventType,
    OtherEvent,
    PanelClient,
    PanelError,
    PanelRequestError,
    PanelResponseError,
    PanelUnavailableError,
    TrafficStrategy,
    UpdateUser,
    UserEvent,
    UserEventType,
    UserStatus,
    parse_event,
    verify_signature,
)
from remnabay.panel._webhooks import sign
from tests.panel_support import user_json

type Handler = Callable[[httpx2.Request], httpx2.Response]

PANEL_URL = "https://panel.example.com"
EXPIRE_AT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def client_with(handler: Handler, base_url: str = PANEL_URL) -> PanelClient:
    return PanelClient(base_url, "secret-token", transport=httpx2.MockTransport(handler))


def respond(payload: Any, status: int = 200) -> Handler:
    return lambda _request: httpx2.Response(status, json={"response": payload})


# --- Недоступность панели (4.30) ---


@pytest.mark.parametrize(
    "error",
    [httpx2.ConnectError("refused"), httpx2.ConnectTimeout("t"), httpx2.ReadTimeout("t")],
    ids=["connect_error", "connect_timeout", "read_timeout"],
)
async def test_4_30_no_connection_or_timeout_is_panel_unavailable(error: Exception) -> None:
    """4.30: соединение не устанавливается или таймаут — панель недоступна."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise error

    async with client_with(handler) as panel:
        with pytest.raises(PanelUnavailableError):
            await panel.get_metadata()


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_4_30_gateway_errors_are_panel_unavailable(status: int) -> None:
    """4.30: ошибки шлюза 502, 503, 504 — панель недоступна."""
    async with client_with(lambda _r: httpx2.Response(status, text="Bad Gateway")) as panel:
        with pytest.raises(PanelUnavailableError):
            await panel.get_metadata()


async def test_4_30_other_errors_are_ordinary_failures() -> None:
    """4.30: 500 на конкретный запрос и обрыв уже начатого ответа — обычный провал."""
    error_body = {"message": "Server error", "errorCode": "A001"}
    async with client_with(lambda _r: httpx2.Response(500, json=error_body)) as panel:
        with pytest.raises(PanelRequestError) as raised:
            await panel.get_metadata()
    assert not isinstance(raised.value, PanelUnavailableError)
    assert (raised.value.status_code, raised.value.error_code) == (500, "A001")

    def broken(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError("Server disconnected without sending a response")

    async with client_with(broken) as panel:
        with pytest.raises(PanelError) as raised_error:
            await panel.get_metadata()
    assert not isinstance(raised_error.value, PanelUnavailableError)


# --- Запросы ---


async def test_requests_carry_token_and_proxy_headers_only_for_plain_http() -> None:
    """Токен — в каждом запросе. Для адреса во внутренней сети (http) клиент добавляет
    заголовки прокси, без которых панель рвёт соединение."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"response": {"version": "3.4.4"}})

    for base_url in ("https://panel.example.com", "http://remnawave:3000"):
        async with client_with(handler, base_url) as panel:
            await panel.get_metadata()

    https_request, http_request = seen
    assert https_request.headers["Authorization"] == "Bearer secret-token"
    assert "X-Forwarded-Proto" not in https_request.headers
    assert http_request.headers["X-Forwarded-Proto"] == "https"
    assert http_request.headers["X-Forwarded-For"]
    assert str(http_request.url) == "http://remnawave:3000/api/system/metadata"


async def test_create_user_sends_only_given_fields() -> None:
    """Создание пользователя: дата — ISO с часовым поясом, незаданные поля не уходят."""
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(201, json={"response": user_json()})

    async with client_with(handler) as panel:
        user = await panel.create_user(
            CreateUser(username="rb_7", expire_at=EXPIRE_AT, hwid_device_limit=3)
        )

    assert bodies == [
        {"username": "rb_7", "expireAt": "2026-11-05T12:00:00Z", "hwidDeviceLimit": 3}
    ]
    assert user.id == 7
    assert user.subscription_url == "https://sub.example.com/abc123"


def test_request_date_must_have_time_zone() -> None:
    """Дату без часового пояса панель поняла бы во времени своего сервера — нельзя."""
    with pytest.raises(ValidationError):
        CreateUser(username="rb_7", expire_at=datetime(2026, 11, 5, 12, 0))


async def test_update_user_sends_null_only_for_clearable_fields() -> None:
    """0042: клиент умеет обнулить поле (`null`); для необнуляемых `None` — «не трогать»."""
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"response": user_json(telegramId=None)})

    async with client_with(handler) as panel:
        await panel.update_user(UpdateUser(id=7, telegram_id=None, expire_at=None))

    assert bodies == [{"id": 7, "telegramId": None}]


async def test_missing_user_is_none_other_errors_raise() -> None:
    """Пользователя нет (404, A063) — `None`; другие ошибки — исключение."""
    not_found = {"message": "User with specified params not found", "errorCode": "A063"}
    async with client_with(lambda _r: httpx2.Response(404, json=not_found)) as panel:
        assert await panel.get_user(7) is None
        assert await panel.get_user_by_username("rb_7") is None

    # 404 без кода панели — например, обратный прокси после смены пути: это ошибка,
    # а не «пользователя нет» (иначе сверка удалила бы все подписки, 4.6)
    async with client_with(lambda _r: httpx2.Response(404, text="<html>Not Found</html>")) as panel:
        with pytest.raises(PanelRequestError):
            await panel.get_user(7)

    forbidden = {"message": "Forbidden", "errorCode": "A004"}
    async with client_with(lambda _r: httpx2.Response(403, json=forbidden)) as panel:
        with pytest.raises(PanelRequestError) as raised:
            await panel.get_user(7)
    assert raised.value.error_code == "A004"


async def test_0042_new_enum_values_and_fields_do_not_break_parsing() -> None:
    """0042: новое значение перечисления и новое поле в ответе не ломают разбор."""
    payload = user_json(status="ARCHIVED", trafficLimitStrategy="YEAR", someNewField=1)

    async with client_with(respond(payload)) as panel:
        user = await panel.get_user(7)

    assert user is not None
    assert user.status is UserStatus.UNKNOWN
    assert user.traffic_limit_strategy is TrafficStrategy.UNKNOWN


async def test_unexpected_response_is_response_error() -> None:
    """Ответ не по модели — понятная ошибка, а не падение где-то дальше."""
    async with client_with(respond({"id": "не число"})) as panel:
        with pytest.raises(PanelResponseError):
            await panel.get_user(7)
    async with client_with(lambda _r: httpx2.Response(200, text="<html>")) as panel:
        with pytest.raises(PanelResponseError):
            await panel.get_metadata()


async def test_user_subpage_config_is_get_with_body() -> None:
    """12.1, 12.16: конфиг пользователя запрашивается GET с заголовками браузера в теле."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = {"subpageConfigUuid": None, "webpageAllowed": True}
        return httpx2.Response(200, json={"response": body})

    async with client_with(handler) as panel:
        config = await panel.get_user_subpage_config("abc123", {"user-agent": "Mozilla/5.0"})

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/subscriptions/subpage-config/abc123"
    assert json.loads(seen[0].content) == {"requestHeaders": {"user-agent": "Mozilla/5.0"}}
    assert config.subpage_config_uuid is None
    assert config.webpage_allowed is True


async def test_devices_are_listed_and_deleted() -> None:
    """6.2, 6.4, 6.5: список устройств пользователя и удаление одного или всех."""
    device = {
        "hwid": "hw-1",
        "userId": 7,
        "platform": "Android",
        "osVersion": "15",
        "deviceModel": "Pixel",
        "userAgent": "Happ/3",
        "requestIp": None,
        "createdAt": "2026-10-05T12:00:00.000Z",
        "updatedAt": "2026-10-05T12:00:00.000Z",
    }
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.content:
            bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"response": {"total": 1, "devices": [device]}})

    async with client_with(handler) as panel:
        devices = await panel.list_devices(7)
        await panel.delete_device(7, "hw-1")
        await panel.delete_all_devices(7)

    assert devices.devices[0].device_model == "Pixel"
    assert bodies == [{"userId": 7, "hwid": "hw-1"}, {"userId": 7}]


# --- Вебхуки (4.1) ---


def test_4_1_signature_must_match_body() -> None:
    """4.1: подпись — HMAC-SHA256 тела секретом; неверная или отсутствующая отклоняется."""
    body = b'{"scope":"user","event":"user.modified"}'
    signature = sign(body, "webhook-secret")

    assert verify_signature(body, signature, "webhook-secret")
    assert verify_signature(body, signature.upper(), "webhook-secret")
    assert not verify_signature(body, signature, "other-secret")
    assert not verify_signature(body + b" ", signature, "webhook-secret")
    assert not verify_signature(body, None, "webhook-secret")
    assert not verify_signature(body, "", "webhook-secret")
    # Заголовок присылает кто угодно: символы вне ASCII — отказ, а не исключение
    assert not verify_signature(body, "éé", "webhook-secret")


def _event_body(scope: str, event: str, data: Any) -> bytes:
    return json.dumps(
        {"scope": scope, "event": event, "timestamp": "2026-10-05T12:00:00.000Z", "data": data}
    ).encode()


def test_user_event_is_parsed() -> None:
    """Событие пользователя несёт пользователя целиком (research/spike-panel.md)."""
    event = parse_event(_event_body("user", "user.expired", user_json(status="EXPIRED")))

    assert isinstance(event, UserEvent)
    assert event.event is UserEventType.EXPIRED
    assert event.data.status is UserStatus.EXPIRED


def test_device_event_is_parsed() -> None:
    """5.7: событие о подключении устройства — пользователь и устройство."""
    device = {"hwid": "hw-1", "userId": 7, "platform": None, "osVersion": None}
    device |= {"deviceModel": None, "userAgent": None, "createdAt": "2026-10-05T12:00:00.000Z"}
    body = _event_body(
        "user_hwid_devices",
        "user_hwid_devices.added",
        {"user": user_json(), "hwidUserDevice": device},
    )

    event = parse_event(body)

    assert isinstance(event, DeviceEvent)
    assert event.event is DeviceEventType.ADDED
    assert event.data.hwid_user_device.hwid == "hw-1"


def test_unneeded_and_unknown_events() -> None:
    """События, не нужные магазину, разбираются без данных; новое имя — `UNKNOWN`."""
    node = parse_event(_event_body("node", "node.connection_lost", {"uuid": "x"}))
    assert isinstance(node, OtherEvent)
    assert node.event == "node.connection_lost"

    future = parse_event(_event_body("user", "user.renamed", user_json()))
    assert isinstance(future, UserEvent)
    assert future.event is UserEventType.UNKNOWN

    with pytest.raises(PanelResponseError):
        parse_event(b"not json")
