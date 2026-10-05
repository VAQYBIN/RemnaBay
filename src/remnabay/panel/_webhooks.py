"""Вебхуки панели: проверка подписи (4.1) и разбор событий.

Панель подписывает тело запроса: HMAC-SHA256 секретом вебхука, в hex, заголовок
`X-Remnawave-Signature` (проверено по коду панели 3.4.4). Заголовок
`X-Remnawave-Timestamp` в подпись не входит, поэтому от повтора он не защищает:
однократность обеспечивает магазин (4.2).
"""

import hashlib
import hmac
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import ValidationError

from remnabay.panel._errors import PanelResponseError
from remnabay.panel._models import Device, PanelModel, User, tolerant

SIGNATURE_HEADER = "X-Remnawave-Signature"


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Подпись верна — событие пришло от панели. Сравнение — за постоянное время."""
    if not signature:
        return False
    # Сравниваются байты: строку с символами вне ASCII compare_digest не принимает,
    # а заголовок присылает кто угодно
    expected = sign(body, secret).encode()
    return hmac.compare_digest(expected, signature.strip().lower().encode())


class UserEventType(StrEnum):
    CREATED = "user.created"
    MODIFIED = "user.modified"
    DELETED = "user.deleted"
    REVOKED = "user.revoked"
    DISABLED = "user.disabled"
    ENABLED = "user.enabled"
    LIMITED = "user.limited"
    EXPIRED = "user.expired"
    TRAFFIC_RESET = "user.traffic_reset"
    FIRST_CONNECTED = "user.first_connected"
    BANDWIDTH_THRESHOLD = "user.bandwidth_usage_threshold_reached"
    NOT_CONNECTED = "user.not_connected"
    EXPIRATION = "user.expiration"
    UNKNOWN = "UNKNOWN"


class DeviceEventType(StrEnum):
    ADDED = "user_hwid_devices.added"
    DELETED = "user_hwid_devices.deleted"
    UNKNOWN = "UNKNOWN"


class UserEvent(PanelModel):
    event: Annotated[UserEventType, tolerant(UserEventType)]
    timestamp: datetime
    data: User


class DeviceEventData(PanelModel):
    user: User
    hwid_user_device: Device


class DeviceEvent(PanelModel):
    event: Annotated[DeviceEventType, tolerant(DeviceEventType)]
    timestamp: datetime
    data: DeviceEventData


class OtherEvent(PanelModel):
    """Событие, которое магазину не нужно (узлы, сервис, CRM и т. п.)."""

    scope: str
    event: str


type PanelEvent = UserEvent | DeviceEvent | OtherEvent


def parse_event(body: bytes) -> PanelEvent:
    """Разбирает тело вебхука с уже проверенной подписью."""
    try:
        payload = json.loads(body)
        scope = payload["scope"]
        if scope == "user":
            return UserEvent.model_validate(payload)
        if scope == "user_hwid_devices":
            return DeviceEvent.model_validate(payload)
        return OtherEvent.model_validate(payload)
    except (ValueError, KeyError, TypeError, ValidationError) as error:
        raise PanelResponseError(f"Непонятное событие панели: {error}") from error
