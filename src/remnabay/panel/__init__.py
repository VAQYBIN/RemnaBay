"""Клиент панели Remnawave (решения 0042, 0027).

Остальной код знает только этот интерфейс: `PanelClient`, модели запросов и
ответов, ошибки и разбор вебхуков. Как устроены запросы, он не знает.
"""

from remnabay.panel._client import ENDPOINTS, PanelClient, required_scopes
from remnabay.panel._errors import (
    PanelError,
    PanelRequestError,
    PanelResponseError,
    PanelUnavailableError,
)
from remnabay.panel._models import (
    CreateUser,
    Device,
    Devices,
    InternalSquad,
    InternalSquads,
    Metadata,
    ResolvedUser,
    ResolveUser,
    SubpageConfig,
    SubscriptionSettings,
    TrafficStrategy,
    UpdateUser,
    User,
    UsersPage,
    UserStatus,
    UserSubpageConfig,
)
from remnabay.panel._webhooks import (
    SIGNATURE_HEADER,
    DeviceEvent,
    DeviceEventType,
    OtherEvent,
    PanelEvent,
    UserEvent,
    UserEventType,
    parse_event,
    verify_signature,
)

# Версии панели, с которыми проверен магазин (0027): контрактный тест прогоняется
# по OpenAPI каждой из них
SUPPORTED_PANEL_VERSIONS = ("3.4.4",)

__all__ = [
    "ENDPOINTS",
    "SIGNATURE_HEADER",
    "SUPPORTED_PANEL_VERSIONS",
    "CreateUser",
    "Device",
    "DeviceEvent",
    "DeviceEventType",
    "Devices",
    "InternalSquad",
    "InternalSquads",
    "Metadata",
    "OtherEvent",
    "PanelClient",
    "PanelError",
    "PanelEvent",
    "PanelRequestError",
    "PanelResponseError",
    "PanelUnavailableError",
    "ResolveUser",
    "ResolvedUser",
    "SubpageConfig",
    "SubscriptionSettings",
    "TrafficStrategy",
    "UpdateUser",
    "User",
    "UserEvent",
    "UserEventType",
    "UserStatus",
    "UserSubpageConfig",
    "UsersPage",
    "parse_event",
    "required_scopes",
    "verify_signature",
]
