"""Чек-лист первичной настройки и открытие магазина (1.7–1.15, сценарий «Первичная настройка»).

Пункты не обучают, а показывают, что ещё не настроено. Обязательные для открытия —
подключение к панели, хотя бы один тариф в продаже, хотя бы один способ оплаты
(1.14). Чек-лист остаётся на главной, пока в нём есть невыполненные пункты (1.15).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from pydantic import JsonValue
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand import AssetKind, asset_versions, brand_name
from remnabay.domain.tariffs import Tariff, TariffState
from remnabay.panel import (
    SUPPORTED_PANEL_VERSIONS,
    PanelClient,
    PanelError,
    PanelRequestError,
    PanelUnavailableError,
    is_compatible_version,
)
from remnabay.panel_sync import webhook_received
from remnabay.shop import (
    SHOP_STATE,
    SHOP_SUPPORT_CONTACT,
    TRIAL_ENABLED,
    ShopState,
    shop_state,
)
from remnabay.shop_settings import get_setting, set_setting

# Чек-лист открывают на главной: ответ панели ждём недолго
PANEL_CHECK_TIMEOUT = timedelta(seconds=5)


class ItemKey(StrEnum):
    PANEL = "panel"
    WEBHOOK = "webhook"
    DEVICE_LIMIT = "device_limit"
    BRAND = "brand"
    TARIFFS = "tariffs"
    PAYMENT = "payment"
    SUPPORT = "support"
    TRIAL = "trial"
    MIGRATION = "migration"


class ItemStatus(StrEnum):
    DONE = "done"
    TODO = "todo"
    # Лимит устройств выключен: предупреждение, открытие не блокирует (1.10)
    WARNING = "warning"
    # Необязательный пункт: миграция (1.12)
    OPTIONAL = "optional"


# Обязательный минимум для открытия (1.14)
REQUIRED = frozenset({ItemKey.PANEL, ItemKey.TARIFFS, ItemKey.PAYMENT})


@dataclass(frozen=True)
class ChecklistItem:
    key: ItemKey
    status: ItemStatus
    details: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    @property
    def required(self) -> bool:
        return self.key in REQUIRED


@dataclass(frozen=True)
class Checklist:
    state: ShopState
    items: list[ChecklistItem]

    @property
    def missing_required(self) -> list[ItemKey]:
        return [i.key for i in self.items if i.required and i.status != ItemStatus.DONE]

    @property
    def can_open(self) -> bool:
        """Кнопка «Открыть магазин» доступна (1.14)."""
        return self.state != ShopState.OPEN and not self.missing_required

    @property
    def complete(self) -> bool:
        """Невыполненных пунктов нет: чек-лист можно не показывать (1.15)."""
        return all(i.status in (ItemStatus.DONE, ItemStatus.OPTIONAL) for i in self.items)


@dataclass(frozen=True)
class _PanelFacts:
    """Что известно о панели: ответ, версия, лимит устройств."""

    error: str | None = None
    version: str | None = None
    hwid_enabled: bool | None = None


async def _panel_facts(panel: PanelClient) -> _PanelFacts:
    try:
        async with asyncio.timeout(PANEL_CHECK_TIMEOUT.total_seconds()):
            metadata, subscription_settings = await asyncio.gather(
                panel.get_metadata(), panel.get_subscription_settings()
            )
    except PanelUnavailableError, TimeoutError:
        return _PanelFacts(error="unavailable")
    except PanelRequestError as error:
        # Токен не подходит или у него нет прав
        unauthorized = error.status_code in (401, 403)
        return _PanelFacts(error="unauthorized" if unauthorized else "error")
    except PanelError:
        return _PanelFacts(error="error")
    hwid = subscription_settings.hwid_settings
    return _PanelFacts(
        version=metadata.version, hwid_enabled=hwid.enabled if hwid is not None else False
    )


def _panel_item(facts: _PanelFacts) -> ChecklistItem:
    """1.8: API отвечает с токеном из `.env` и версия совместима; иначе — требуемая версия."""
    details: dict[str, JsonValue] = {
        "supported_versions": list(SUPPORTED_PANEL_VERSIONS),
        "version": facts.version,
        "error": facts.error,
    }
    compatible = facts.version is not None and is_compatible_version(facts.version)
    if facts.version is not None and not compatible:
        details["error"] = "incompatible_version"
    return ChecklistItem(ItemKey.PANEL, ItemStatus.DONE if compatible else ItemStatus.TODO, details)


def _device_limit_item(facts: _PanelFacts) -> ChecklistItem:
    """1.10: выключенный лимит устройств — предупреждение о защите триала, не блокирует."""
    if facts.hwid_enabled is None:
        return ChecklistItem(ItemKey.DEVICE_LIMIT, ItemStatus.TODO, {"error": "panel"})
    if facts.hwid_enabled:
        return ChecklistItem(ItemKey.DEVICE_LIMIT, ItemStatus.DONE)
    return ChecklistItem(ItemKey.DEVICE_LIMIT, ItemStatus.WARNING)


async def payment_methods_ready(session: AsyncSession) -> bool:
    """Есть хотя бы один способ оплаты (1.14). Провайдеры и их ключи — блок 3:
    до него способов оплаты нет."""
    del session
    return False


async def build_checklist(
    session: AsyncSession,
    panel: PanelClient,
    *,
    webhook_url: str,
    webhook_secret: str | None,
) -> Checklist:
    """Состояние каждого пункта (1.7). `webhook_secret` — только для владельца."""
    facts = await _panel_facts(panel)
    received = await webhook_received(session)
    versions = await asset_versions(session)
    name = await brand_name(session)
    has_mark = AssetKind.MARK in versions
    on_sale = await session.scalar(select(exists().where(Tariff.state == TariffState.ON_SALE)))
    support = (await get_setting(session, SHOP_SUPPORT_CONTACT)).strip()
    items = [
        _panel_item(facts),
        # 1.9: адрес и секрет для панели; выполнен после первого события с верной подписью
        ChecklistItem(
            ItemKey.WEBHOOK,
            ItemStatus.DONE if received else ItemStatus.TODO,
            {"url": webhook_url, "secret": webhook_secret},
        ),
        _device_limit_item(facts),
        # 1.11: название и квадратный знак
        ChecklistItem(
            ItemKey.BRAND,
            ItemStatus.DONE if name and has_mark else ItemStatus.TODO,
            {"name": bool(name), "mark": has_mark},
        ),
        ChecklistItem(ItemKey.TARIFFS, ItemStatus.DONE if on_sale else ItemStatus.TODO),
        ChecklistItem(
            ItemKey.PAYMENT,
            ItemStatus.DONE if await payment_methods_ready(session) else ItemStatus.TODO,
        ),
        ChecklistItem(ItemKey.SUPPORT, ItemStatus.DONE if support else ItemStatus.TODO),
        # 1.7: информационный пункт, всегда выполнен — оба варианта нормальны
        ChecklistItem(
            ItemKey.TRIAL,
            ItemStatus.DONE,
            {"enabled": await get_setting(session, TRIAL_ENABLED)},
        ),
        # 1.12: необязательный; ведёт к усыновлению или импорту (блок 9)
        ChecklistItem(ItemKey.MIGRATION, ItemStatus.OPTIONAL),
    ]
    return Checklist(state=await shop_state(session), items=items)


class ShopNotReadyError(Exception):
    """Обязательные пункты не выполнены: магазин не открывается (1.14)."""

    def __init__(self, missing: list[ItemKey]) -> None:
        self.missing = missing
        super().__init__(", ".join(missing))


async def open_shop(session: AsyncSession, checklist: Checklist, *, member_id: int) -> None:
    """Открыть магазин — по нажатию владельца (1.14); и после временного закрытия (1.22)."""
    if checklist.missing_required:
        raise ShopNotReadyError(checklist.missing_required)
    if await shop_state(session) != ShopState.OPEN:
        await set_setting(session, SHOP_STATE, ShopState.OPEN, member_id=member_id)


async def close_shop(session: AsyncSession, *, member_id: int) -> None:
    """Временно закрыть открытый магазин (1.22)."""
    if await shop_state(session) == ShopState.OPEN:
        await set_setting(session, SHOP_STATE, ShopState.PAUSED, member_id=member_id)
