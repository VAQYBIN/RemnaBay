"""Устройства подписки: список из панели и удаление (блок 6, сценарий «Управление устройствами»).

- Раздел «Устройства» есть, только если в панели включён лимит устройств (6.1).
  Ответ панели магазин запоминает: пока панель недоступна, действует последний.
- Список и лимит читаются из панели при открытии раздела (6.2, 6.3). Своих данных
  об устройствах у магазина нет — только реестр подключений для триала (5.7).
- Удаление идёт через панель и пишется в журнал (6.4). Панель недоступна —
  удаление ставится в повтор (6.6) и выполняется, когда панель вернётся.
- Удаление устройства не удаляет запись реестра (5.9).
"""

import asyncio
import hashlib
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.subscriptions import Subscription, operations_key
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import tell_action_failed
from remnabay.panel import Device, PanelClient, PanelUnavailableError, SubscriptionSettings
from remnabay.panel_sync import panel_available
from remnabay.queue import TaskContext, task
from remnabay.shop_settings import ShopSetting, remember_setting

SUBSCRIPTION_SUBJECT = "subscription"
# Короткий отпечаток HWID для кнопки: данные кнопки Telegram — не длиннее 64 байт
_HANDLE_LENGTH = 12

# Включён ли в панели лимит устройств — последний ответ панели (6.1)
HWID_ENABLED = ShopSetting("panel.hwid_enabled", TypeAdapter[bool | None](bool | None), None)


def handle(hwid: str) -> str:
    """Отпечаток устройства для данных кнопки «Удалить»."""
    return hashlib.sha256(hwid.encode()).hexdigest()[:_HANDLE_LENGTH]


def app_name(device: Device) -> str | None:
    """Приложение устройства — из того, что панель знает о нём: название в User-Agent
    приложения («Happ/2.6.0/ios» → «Happ», решение 0058)."""
    agent = (device.user_agent or "").strip()
    if not agent:
        return None
    name = agent.split("/", 1)[0].split(" ", 1)[0].strip()
    return name or None


@dataclass(frozen=True)
class DeviceLimit:
    """Лимит устройств в панели: включён ли и лимит по умолчанию для пользователя без своего."""

    enabled: bool
    fallback: int | None = None


async def remember_device_limit(
    session: AsyncSession, settings: SubscriptionSettings
) -> DeviceLimit:
    """6.1: включён ли в панели лимит устройств — по ответу панели; ответ запоминается
    (`HWID_ENABLED`): он действует, пока панель не отвечает."""
    hwid = settings.hwid_settings
    enabled = hwid is not None and hwid.enabled
    await remember_setting(session, HWID_ENABLED, enabled)
    return DeviceLimit(enabled=enabled, fallback=hwid.fallback_device_limit if hwid else None)


@dataclass(frozen=True)
class DeviceList:
    """Устройства подписки и лимит из панели (6.2)."""

    devices: list[Device]
    limit: int | None

    @property
    def used(self) -> int:
        return len(self.devices)

    def find(self, device_handle: str) -> Device | None:
        return next((d for d in self.devices if handle(d.hwid) == device_handle), None)


async def list_devices(
    panel: PanelClient, subscription: Subscription, limit: DeviceLimit
) -> DeviceList:
    """6.2, 6.3: список и лимит читаются из панели. Ошибки панели — у вызывающего."""
    user, devices = await asyncio.gather(
        panel.get_user(subscription.panel_user_id),
        panel.list_devices(subscription.panel_user_id),
    )
    own = user.hwid_device_limit if user is not None else subscription.device_limit
    ordered = sorted(devices.devices, key=lambda device: device.created_at)
    return DeviceList(devices=ordered, limit=own if own is not None else limit.fallback)


class Removal(StrEnum):
    # Удалено в панели, список обновился (6.4)
    DONE = "done"
    # Панель недоступна — удаление в очереди (6.6)
    QUEUED = "queued"


class DeleteArgs(BaseModel):
    subscription_id: int
    client_id: int
    # Устройство: HWID или, если панель не ответила и HWID не узнать, — отпечаток с
    # кнопки «Удалить» (6.6). Оба пусты — удалить все устройства подписки (6.5)
    hwid: str | None = None
    device: str | None = None
    # Сбросил участник команды из карточки клиента (10.5); пусто — сам клиент
    member_id: int | None = None

    @property
    def all_devices(self) -> bool:
        return self.hwid is None and self.device is None

    @property
    def actor(self) -> Actor:
        if self.member_id is not None:
            return Actor.team_member(self.member_id)
        return Actor.client(self.client_id)


async def _journal_removed(session: AsyncSession, args: DeleteArgs, *, queued: bool) -> None:
    await record(
        session,
        actor=args.actor,
        action="device.deleted_all" if args.all_devices else "device.deleted",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, args.subscription_id),
        details={"hwid": args.hwid, "device": args.device, "queued": queued},
    )


async def _delete(panel: PanelClient, subscription: Subscription, args: DeleteArgs) -> str | None:
    """Удаляет в панели; возвращает HWID удалённого устройства (`None` — все или уже нет)."""
    user_id = subscription.panel_user_id
    if args.all_devices:
        await panel.delete_all_devices(user_id)
        return None
    hwid = args.hwid
    if hwid is None:
        devices = await panel.list_devices(user_id)
        found = next((d for d in devices.devices if handle(d.hwid) == args.device), None)
        if found is None:
            # Устройства уже нет — удалять нечего
            return None
        hwid = found.hwid
    await panel.delete_device(user_id, hwid)
    return hwid


async def remove_devices(
    session: AsyncSession,
    panel: PanelClient,
    subscription: Subscription,
    *,
    hwid: str | None = None,
    device: str | None = None,
    member_id: int | None = None,
) -> Removal:
    """Клиент удаляет устройство или все сразу (6.4, 6.5); участник команды сбрасывает
    все устройства подписки (10.5, `member_id`). Панель недоступна — удаление ставится
    в повтор (6.6). Действие — в журнал."""
    args = DeleteArgs(
        subscription_id=subscription.id,
        client_id=subscription.client_id,
        hwid=hwid,
        device=device,
        member_id=member_id,
    )
    if await panel_available(session):
        try:
            await _delete(panel, subscription, args)
        except PanelUnavailableError:
            pass
        else:
            await _journal_removed(session, args, queued=False)
            return Removal.DONE
    await delete_devices.enqueue(session, args, key=operations_key(subscription.id))
    await _journal_removed(session, args, queued=True)
    return Removal.QUEUED


@task("devices.delete", DeleteArgs)
async def delete_devices(context: TaskContext, args: DeleteArgs) -> None:
    """Удаление устройства, отложенное до возвращения панели (6.6). Повтор безопасен:
    удалённого устройства в панели уже нет."""
    subscription = await context.session.get(Subscription, args.subscription_id)
    if subscription is None or subscription.deleted_at is not None:
        return
    hwid = await _delete(runtime.current().panel, subscription, args)
    await record(
        context.session,
        actor=Actor.SYSTEM,
        action="device.deleted_in_panel",
        outcome=Outcome.SUCCESS,
        subject=Subject(SUBSCRIPTION_SUBJECT, args.subscription_id),
        details={"hwid": hwid, "all": args.all_devices},
    )


@delete_devices.on_cancelled
async def _delete_cancelled(session: AsyncSession, _task_id: int, args: DeleteArgs) -> None:
    """4.32: команда отменила удаление, которое начал клиент, — он получает «Не удалось
    выполнить действие». Сброс устройств из карточки клиента начала команда: клиенту
    ничего не отправляется (0067)."""
    if args.member_id is None:
        await tell_action_failed(session, args.client_id)
