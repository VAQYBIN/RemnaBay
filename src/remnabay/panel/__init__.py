"""Клиент панели Remnawave (решения 0042, 0027).

Остальной код знает только этот интерфейс: `PanelClient`, модели запросов и
ответов, ошибки и разбор вебхуков. Как устроены запросы, он не знает.
"""

import re

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

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse_version(version: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(version.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_compatible_version(version: str) -> bool:
    """Совместима ли версия панели (1.8, решение 0051): та же мажорная и минорная
    версия, что у поддерживаемой, и патч не ниже. Исправления панели магазин
    не ломают; новая минорная версия — только после контрактного теста."""
    parsed = _parse_version(version)
    if parsed is None:
        return False
    for supported in SUPPORTED_PANEL_VERSIONS:
        minimum = _parse_version(supported)
        if minimum is not None and parsed[:2] == minimum[:2] and parsed[2] >= minimum[2]:
            return True
    return False


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
    "is_compatible_version",
    "parse_event",
    "required_scopes",
    "verify_signature",
]
