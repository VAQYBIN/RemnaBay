"""Тонкий клиент панели Remnawave на httpx2 (решение 0042)."""

from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx2
from pydantic import ValidationError

from remnabay.journal import JsonValue
from remnabay.panel._errors import (
    PanelError,
    PanelRequestError,
    PanelResponseError,
    PanelUnavailableError,
)
from remnabay.panel._models import (
    CreateUser,
    DeleteAllDevices,
    DeleteDevice,
    Devices,
    InternalSquads,
    Metadata,
    PanelModel,
    PanelRequest,
    ResolvedUser,
    ResolveUser,
    SubpageConfig,
    SubpageConfigRequest,
    SubscriptionSettings,
    UpdateUser,
    User,
    UsersPage,
    UserSubpageConfig,
)

# Запрос к панели внутри задачи — с коротким таймаутом (0036)
DEFAULT_TIMEOUT = timedelta(seconds=10)
# Ошибки шлюза — тоже недоступность панели (4.30)
GATEWAY_ERRORS = frozenset({502, 503, 504})
# «Пользователь не найден» (код ошибки панели A063)
NOT_FOUND = 404
# Панель рвёт соединение без этих заголовков, если запрос пришёл не через
# обратный прокси с HTTPS — так бывает при адресе во внутренней Docker-сети
PROXY_HEADERS = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}


@dataclass(frozen=True)
class Endpoint[R: PanelModel]:
    """Запрос к панели: метод, путь как в OpenAPI, нужное API-токену право и модели.

    Таблица запросов — общая для клиента и контрактного теста.
    """

    method: str
    path: str
    scope: str
    response: type[R]
    request: type[PanelRequest] | None = None


METADATA = Endpoint("GET", "/api/system/metadata", "system:metadata", Metadata)
SUBSCRIPTION_SETTINGS = Endpoint(
    "GET", "/api/subscription-settings", "subscription-settings:get", SubscriptionSettings
)
INTERNAL_SQUADS = Endpoint("GET", "/api/internal-squads", "internal-squads:list", InternalSquads)
CREATE_USER = Endpoint("POST", "/api/users", "users:create", User, CreateUser)
GET_USER = Endpoint("GET", "/api/users/{userId}", "users:by-id", User)
GET_USER_BY_USERNAME = Endpoint(
    "GET", "/api/users/by-username/{username}", "users:by-username", User
)
UPDATE_USER = Endpoint("PATCH", "/api/users", "users:update", User, UpdateUser)
LIST_USERS = Endpoint("GET", "/api/users", "users:list", UsersPage)
RESOLVE_USER = Endpoint("POST", "/api/users/resolve", "users:resolve", ResolvedUser, ResolveUser)
RESET_TRAFFIC = Endpoint(
    "POST", "/api/users/{userId}/actions/reset-traffic", "users:reset-traffic", User
)
LIST_DEVICES = Endpoint(
    "GET", "/api/hwid/devices/{userId}", "hwid-user-devices:list-by-user", Devices
)
DELETE_DEVICE = Endpoint(
    "POST", "/api/hwid/devices/delete", "hwid-user-devices:delete", Devices, DeleteDevice
)
DELETE_ALL_DEVICES = Endpoint(
    "POST",
    "/api/hwid/devices/delete-all",
    "hwid-user-devices:delete-all",
    Devices,
    DeleteAllDevices,
)
USER_SUBPAGE_CONFIG = Endpoint(
    "GET",
    "/api/subscriptions/subpage-config/{shortUuid}",
    "subscriptions:subpage-config",
    UserSubpageConfig,
    SubpageConfigRequest,
)
SUBPAGE_CONFIG = Endpoint(
    "GET", "/api/subscription-page-configs/{uuid}", "subscription-page-configs:get", SubpageConfig
)

ENDPOINTS = (
    METADATA,
    SUBSCRIPTION_SETTINGS,
    INTERNAL_SQUADS,
    CREATE_USER,
    GET_USER,
    GET_USER_BY_USERNAME,
    UPDATE_USER,
    LIST_USERS,
    RESOLVE_USER,
    RESET_TRAFFIC,
    LIST_DEVICES,
    DELETE_DEVICE,
    DELETE_ALL_DEVICES,
    USER_SUBPAGE_CONFIG,
    SUBPAGE_CONFIG,
)


def required_scopes() -> list[str]:
    """Права API-токена, которых достаточно магазину (для документации оператора)."""
    return sorted({endpoint.scope for endpoint in ENDPOINTS})


def _error_details(response: httpx2.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:500]
    if isinstance(body, dict):
        error_code = body.get("errorCode")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        message = body.get("message")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return (
            error_code if isinstance(error_code, str) else None,
            message if isinstance(message, str) else response.text[:500],
        )
    return None, response.text[:500]


class PanelClient:
    """Клиент панели. Остальной код знает только его методы и модели (0042).

    Ошибки — `PanelUnavailableError` (панель недоступна, 4.30), `PanelRequestError`
    (панель ответила ошибкой), `PanelResponseError` (непонятный ответ), прочие сбои
    сети — `PanelError`.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: timedelta = DEFAULT_TIMEOUT,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "RemnaBay"}
        if base_url.startswith("http://"):
            headers |= PROXY_HEADERS
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout.total_seconds(),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call[R: PanelModel](
        self,
        endpoint: Endpoint[R],
        *,
        path: dict[str, str | int | UUID] | None = None,
        body: PanelRequest | None = None,
        query: dict[str, int] | None = None,
    ) -> R:
        url = endpoint.path.format(**(path or {}))
        json: dict[str, JsonValue] | None = body.to_json() if body is not None else None
        try:
            response = await self._http.request(endpoint.method, url, json=json, params=query)
        except (httpx2.TimeoutException, httpx2.ConnectError) as error:
            raise PanelUnavailableError(f"Панель недоступна: {error!r}") from error
        except httpx2.HTTPError as error:
            raise PanelError(f"Сбой запроса к панели: {error!r}") from error

        if response.status_code in GATEWAY_ERRORS:
            raise PanelUnavailableError(f"Панель недоступна: ответ {response.status_code}")
        if response.is_error:
            error_code, message = _error_details(response)
            raise PanelRequestError(response.status_code, error_code, message)
        try:
            envelope = response.json()
            return endpoint.response.model_validate(envelope["response"])
        except (ValueError, KeyError, TypeError, ValidationError) as error:
            raise PanelResponseError(f"Непонятный ответ панели на {url}: {error}") from error

    async def _call_or_none[R: PanelModel](
        self, endpoint: Endpoint[R], *, path: dict[str, str | int | UUID]
    ) -> R | None:
        try:
            return await self._call(endpoint, path=path)
        except PanelRequestError as error:
            if error.status_code == NOT_FOUND:
                return None
            raise

    async def get_metadata(self) -> Metadata:
        """Версия панели — для проверки совместимости (1.8)."""
        return await self._call(METADATA)

    async def get_subscription_settings(self) -> SubscriptionSettings:
        """Включён ли лимит устройств в панели (1.10, 6.1)."""
        return await self._call(SUBSCRIPTION_SETTINGS)

    async def list_internal_squads(self) -> InternalSquads:
        """Сквады для тарифа (2.2)."""
        return await self._call(INTERNAL_SQUADS)

    async def create_user(self, request: CreateUser) -> User:
        return await self._call(CREATE_USER, body=request)

    async def get_user(self, user_id: int) -> User | None:
        """Пользователь или `None`, если его нет (например, удалён в панели, 4.6)."""
        return await self._call_or_none(GET_USER, path={"userId": user_id})

    async def get_user_by_username(self, username: str) -> User | None:
        """Проверка «не создан ли уже» перед созданием пользователя (4.15)."""
        return await self._call_or_none(GET_USER_BY_USERNAME, path={"username": username})

    async def update_user(self, request: UpdateUser) -> User:
        return await self._call(UPDATE_USER, body=request)

    async def list_users(self, *, start: int, size: int) -> UsersPage:
        """Страница пользователей — для сверки (4.8) и усыновления (блок 9)."""
        return await self._call(LIST_USERS, query={"start": start, "size": size})

    async def resolve_user(self, request: ResolveUser) -> ResolvedUser | None:
        """`id`, `shortUuid` и `username` по любому из них (migration-format.md)."""
        try:
            return await self._call(RESOLVE_USER, body=request)
        except PanelRequestError as error:
            if error.status_code == NOT_FOUND:
                return None
            raise

    async def reset_traffic(self, user_id: int) -> User:
        """Обнуление счётчика трафика (0010, 3.16)."""
        return await self._call(RESET_TRAFFIC, path={"userId": user_id})

    async def list_devices(self, user_id: int) -> Devices:
        return await self._call(LIST_DEVICES, path={"userId": user_id})

    async def delete_device(self, user_id: int, hwid: str) -> Devices:
        return await self._call(DELETE_DEVICE, body=DeleteDevice(user_id=user_id, hwid=hwid))

    async def delete_all_devices(self, user_id: int) -> Devices:
        return await self._call(DELETE_ALL_DEVICES, body=DeleteAllDevices(user_id=user_id))

    async def get_user_subpage_config(
        self, short_uuid: str, request_headers: dict[str, str]
    ) -> UserSubpageConfig:
        """Конфиг Subscription Page пользователя и пускает ли панель браузер (12.1, 12.16).

        GET с телом запроса — так устроен этот запрос в панели.
        """
        return await self._call(
            USER_SUBPAGE_CONFIG,
            path={"shortUuid": short_uuid},
            body=SubpageConfigRequest(request_headers=request_headers),
        )

    async def get_subpage_config(self, config_uuid: UUID) -> SubpageConfig:
        return await self._call(SUBPAGE_CONFIG, path={"uuid": config_uuid})
