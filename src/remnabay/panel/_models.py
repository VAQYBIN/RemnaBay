"""Модели запросов и ответов панели — только то, что нужно магазину (0042).

Модели написаны вручную по контракту панели 3.4.4; контрактный тест сверяет их
с `openapi.json` каждой поддерживаемой версии. Правила:
- неизвестные поля ответа игнорируются — новая версия панели их добавляет;
- неизвестное значение перечисления становится `UNKNOWN`, а не ломает разбор;
- в запросах отправляются только явно заданные поля, поэтому `None` уходит
  в панель как `null` и обнуляет поле.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_camel

from remnabay.journal import JsonValue


class PanelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        frozen=True,
    )


def tolerant[E: StrEnum](enum_class: type[E]) -> BeforeValidator:
    """Неизвестное значение перечисления становится `UNKNOWN`."""
    known = {member.value for member in enum_class}

    def to_known(value: Any) -> Any:
        return value if value in known else "UNKNOWN"

    return BeforeValidator(to_known)


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    LIMITED = "LIMITED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class TrafficStrategy(StrEnum):
    NO_RESET = "NO_RESET"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    # Раз в месяц в день создания пользователя — «раз в месяц от даты создания» (0010)
    MONTH_ROLLING = "MONTH_ROLLING"
    UNKNOWN = "UNKNOWN"


type Status = Annotated[UserStatus, tolerant(UserStatus)]
type Strategy = Annotated[TrafficStrategy, tolerant(TrafficStrategy)]


# --- Ответы ---


class Metadata(PanelModel):
    version: str


class HwidSettings(PanelModel):
    enabled: bool


class SubscriptionSettings(PanelModel):
    # Пусто — лимит устройств в панели не настроен (1.10, 6.1)
    hwid_settings: HwidSettings | None


class InternalSquad(PanelModel):
    uuid: UUID
    name: str


class InternalSquads(PanelModel):
    total: int
    internal_squads: list[InternalSquad]


class SquadRef(PanelModel):
    uuid: UUID
    name: str


class UserTraffic(PanelModel):
    used_traffic_bytes: int
    first_connected_at: datetime | None
    online_at: datetime | None


class User(PanelModel):
    """Пользователь панели. В 3.4.4 адресуется числовым `id`; ссылка — по `shortUuid`."""

    id: int
    short_uuid: str
    username: str
    status: Status
    traffic_limit_bytes: int
    traffic_limit_strategy: Strategy
    expire_at: datetime
    telegram_id: int | None
    description: str | None
    hwid_device_limit: int | None
    external_squad_uuid: UUID | None
    sub_revoked_at: datetime | None
    last_traffic_reset_at: datetime | None
    created_at: datetime
    subscription_url: str
    active_internal_squads: list[SquadRef]
    user_traffic: UserTraffic


class UsersPage(PanelModel):
    users: list[User]
    total: int


class ResolvedUser(PanelModel):
    id: int
    short_uuid: str
    username: str


class Device(PanelModel):
    hwid: str
    user_id: int
    platform: str | None
    os_version: str | None
    device_model: str | None
    user_agent: str | None
    created_at: datetime


class Devices(PanelModel):
    total: int
    devices: list[Device]


class UserSubpageConfig(PanelModel):
    # Пусто — и «конфиг по умолчанию», и «пользователь не найден» (research/spike-panel.md)
    subpage_config_uuid: UUID | None
    webpage_allowed: bool


class SubpageConfig(PanelModel):
    """Конфиг Subscription Page. Содержимое разбирает инструкция (блок 12)."""

    uuid: UUID
    name: str
    config: dict[str, JsonValue] | None


# --- Запросы ---


class PanelRequest(PanelModel):
    # Поля, которые панель позволяет обнулить: для них `None` уходит как `null`.
    # Для остальных `None` значит «не отправлять». Контрактный тест сверяет список
    NULLABLE: ClassVar[frozenset[str]] = frozenset()

    def to_json(self) -> dict[str, JsonValue]:
        """Только явно заданные поля; `null` — только для обнуляемых."""
        data = self.model_dump(mode="json", exclude_unset=True)
        return {
            to_camel(name): value
            for name, value in data.items()
            if value is not None or name in self.NULLABLE
        }


class CreateUser(PanelRequest):
    NULLABLE = frozenset({"telegram_id"})

    username: str
    # Только с часовым поясом: дату без пояса панель поняла бы во времени своего сервера
    expire_at: AwareDatetime
    traffic_limit_bytes: int | None = None
    traffic_limit_strategy: TrafficStrategy | None = None
    hwid_device_limit: int | None = None
    active_internal_squads: list[UUID] | None = None
    telegram_id: int | None = None
    description: str | None = None


class UpdateUser(PanelRequest):
    """Изменение пользователя. Неуказанные поля панель не трогает; `None` в
    обнуляемом поле обнуляет его (например, `telegram_id`)."""

    NULLABLE = frozenset({"telegram_id", "description", "hwid_device_limit", "external_squad_uuid"})

    id: int
    expire_at: AwareDatetime | None = None
    traffic_limit_bytes: int | None = None
    traffic_limit_strategy: TrafficStrategy | None = None
    hwid_device_limit: int | None = None
    active_internal_squads: list[UUID] | None = None
    telegram_id: int | None = None
    description: str | None = None
    external_squad_uuid: UUID | None = None


class ResolveUser(PanelRequest):
    id: int | None = None
    short_uuid: str | None = None
    username: str | None = None


class DeleteDevice(PanelRequest):
    user_id: int
    hwid: str


class DeleteAllDevices(PanelRequest):
    user_id: int


class SubpageConfigRequest(PanelRequest):
    request_headers: dict[str, str]
