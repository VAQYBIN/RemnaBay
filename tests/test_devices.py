"""Реестр устройств и раздел «Устройства» (блоки 5 и 6; 5.7–5.9, 6.1–6.7; решение 0058)."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from aiogram.methods import EditMessageText, SendMessage, SendRichMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.bot._common import AskRemoveAll, Card, DevicesScreen, RemoveAll, RemoveDevice
from remnabay.devices import DeleteArgs, delete_devices, handle
from remnabay.domain.clients import Client
from remnabay.domain.subscriptions import PanelUserStatus, Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.domain.trials import DeviceRecord
from remnabay.journal import Subject, entries_for
from remnabay.panel import parse_event
from remnabay.panel_sync import ReconcileArgs, Source, receive_event, reconcile
from remnabay.panel_sync._outage import PanelOutage
from remnabay.shop import SHOP_STATE, ShopState
from tests.bot_support import telegram_user
from tests.domain_support import add, make_client, make_subscription, put_setting
from tests.guide_support import queued, run_queued
from tests.panel_support import device_json, fake_runtime, user_json
from tests.web_support import Shop, running_shop

USER = telegram_user(777)

type Screen = SendMessage | SendRichMessage | EditMessageText


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
        yield shop


async def _subscription(
    shop: Shop, *, client: Client | None = None, user_id: int = 7, name: str = "Основная"
) -> Subscription:
    if client is None:
        client = await make_client(shop.session, telegram_id=USER.id)
    subscription = make_subscription(client, None, panel_user_id=user_id)
    subscription.name = name
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.expires_at = datetime(2026, 12, 1, tzinfo=UTC)
    await add(shop.session, subscription)
    shop.panel.add_user(id=user_id, hwidDeviceLimit=3, shortUuid=subscription.panel_short_uuid)
    return subscription


def _labels(screen: Screen) -> list[str]:
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


def _buttons(screen: Screen) -> list[InlineKeyboardButton]:
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button for row in markup.inline_keyboard for button in row]


def _text(screen: Screen) -> str:
    assert not isinstance(screen, SendRichMessage)
    return screen.text or ""


async def _open(shop: Shop, subscription: Subscription) -> Screen:
    await shop.press(USER, DevicesScreen(subscription_id=subscription.id).pack())
    return shop.telegram.last()


async def _records(session: AsyncSession) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(DeviceRecord.hwid, DeviceRecord.subscription_id).order_by(DeviceRecord.id)
    )
    return [(hwid, subscription_id) for hwid, subscription_id in rows]


# --- 5.7–5.9. Реестр устройств ---


def _device_event(user: dict[str, Any], device: dict[str, Any], event: str) -> bytes:
    payload = {
        "scope": "user_hwid_devices",
        "event": event,
        "timestamp": "2026-10-11T10:00:00.000Z",
        "data": {"user": user, "hwidUserDevice": device},
    }
    return json.dumps(payload).encode()


async def test_5_7_device_added_event_goes_to_registry(shop: Shop) -> None:
    """5.7: панель сообщила о подключении устройства к подписке — в реестре HWID,
    клиент, подписка, дата подключения; повтор события второй записи не даёт."""
    subscription = await _subscription(shop)
    body = _device_event(user_json(id=7), device_json(hwid="h-1"), "user_hwid_devices.added")

    await receive_event(shop.session, body, parse_event(body))
    await receive_event(shop.session, body, parse_event(body))

    [record] = await shop.session.scalars(select(DeviceRecord))
    assert (record.hwid, record.client_id, record.subscription_id) == (
        "h-1",
        subscription.client_id,
        subscription.id,
    )
    assert record.connected_at == datetime(2026, 10, 5, 12, tzinfo=UTC)


async def test_5_8_reconcile_fills_registry_for_trial_only(shop: Shop) -> None:
    """5.8: событие о триальной подписке не дошло — сверка запрашивает устройства
    пользователя; для платных подписок реестр ведётся только по событиям."""
    trial = await _subscription(shop)
    trial.is_trial = True
    other_client = await make_client(shop.session, telegram_id=778)
    paid = await _subscription(shop, client=other_client, user_id=8)
    shop.panel.devices = {7: [device_json(hwid="t-1")], 8: [device_json(hwid="p-1", userId=8)]}

    with fake_runtime(panel=shop.panel):
        for subscription in (trial, paid):
            args = ReconcileArgs(subscription_id=subscription.id, source=Source.SYNC)
            await reconcile(shop.session, subscription, args)

    assert await _records(shop.session) == [("t-1", trial.id)]


async def test_5_9_deleting_device_keeps_registry_record(shop: Shop) -> None:
    """5.9: удаление устройства клиентом или в панели не удаляет запись реестра."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json(hwid="h-1")]}
    added = _device_event(user_json(id=7), device_json(hwid="h-1"), "user_hwid_devices.added")
    await receive_event(shop.session, added, parse_event(added))

    await _open(shop, subscription)
    await shop.press(USER, RemoveAll(subscription_id=subscription.id).pack())
    deleted = _device_event(user_json(id=7), device_json(hwid="h-1"), "user_hwid_devices.deleted")
    await receive_event(shop.session, deleted, parse_event(deleted))

    assert shop.panel.devices[7] == []
    assert await _records(shop.session) == [("h-1", subscription.id)]


# --- 6.1–6.7. Раздел «Устройства» ---


async def test_6_1_no_devices_section_when_panel_limit_off(shop: Shop) -> None:
    """6.1: лимит устройств в панели выключен — в карточке нет «Устройства», а старая
    кнопка раздела ведёт в карточку."""
    subscription = await _subscription(shop)
    shop.panel.hwid_enabled = False
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    card = shop.telegram.last()
    assert "Устройства" not in _labels(card)
    reopened = await _open(shop, subscription)
    assert isinstance(reopened, EditMessageText)
    assert reopened.rich_message is not None
    assert "Устройства" not in _labels(reopened)


async def test_6_2_6_3_list_from_panel_with_used_and_limit(shop: Shop) -> None:
    """6.2, 6.3: платформа, модель, приложение, дата подключения; «занято X из Y» с
    лимитом из панели; список читается из панели при каждом открытии."""
    subscription = await _subscription(shop)
    shop.panel.devices = {
        7: [
            device_json(hwid="h-1"),
            device_json(
                hwid="h-2",
                platform="Android",
                deviceModel="Pixel 8",
                userAgent="v2rayNG/1.9.0",
                createdAt="2026-10-06T12:00:00.000Z",
            ),
        ]
    }
    screen = await _open(shop, subscription)
    text = _text(screen)
    assert "Устройства подписки «Основная»\nЗанято 2 из 3." in text
    assert "iOS · iPhone 15 · Happ · с 5 октября 2026, 15:00 (МСК)" in text
    assert "Android · Pixel 8 · v2rayNG · с " in text
    assert isinstance(screen, EditMessageText)
    assert [entity.type for entity in screen.entities or []] == ["date_time", "date_time"]
    assert _labels(screen) == [
        "Удалить iOS iPhone 15",
        "Удалить Android Pixel 8",
        "Удалить все",
        "Назад",
    ]
    assert {b.style for b in _buttons(screen)[:3]} == {"danger"}

    shop.panel.devices[7] = []
    empty = await _open(shop, subscription)
    assert "Занято 0 из 3." in _text(empty)
    assert "К подписке пока не подключено ни одного устройства." in _text(empty)


async def test_6_2_unknown_values_are_dashes(shop: Shop) -> None:
    """6.2, решение 0059: чего панель не знает об устройстве, показывается прочерком."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json(platform=None, deviceModel=None, userAgent=None)]}
    text = _text(await _open(shop, subscription))
    assert "— · — · — · с " in text


async def test_6_2_limit_falls_back_to_panel_default(shop: Shop) -> None:
    """6.2: у пользователя нет своего лимита — лимит по умолчанию из панели."""
    subscription = await _subscription(shop)
    shop.panel.users[7]["hwidDeviceLimit"] = None
    shop.panel.fallback_device_limit = 5
    assert "Занято 0 из 5." in _text(await _open(shop, subscription))


async def test_6_4_delete_device_in_panel_and_journal(shop: Shop) -> None:
    """6.4: клиент удалил устройство — оно удалено в панели, список обновлён, действие
    в журнале."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json(hwid="h-1"), device_json(hwid="h-2")]}
    await _open(shop, subscription)

    remove = RemoveDevice(subscription_id=subscription.id, device=handle("h-1"))
    await shop.press(USER, remove.pack())

    screen = shop.telegram.last()
    assert [d["hwid"] for d in shop.panel.devices[7]] == ["h-2"]
    assert _text(screen).startswith("Устройство удалено, место освободилось.")
    assert "Занято 1 из 3." in _text(screen)
    [entry] = [
        e
        for e in await entries_for(shop.session, Subject("subscription", subscription.id))
        if e.action == "device.deleted"
    ]
    assert entry.actor_ref == str(subscription.client_id)
    assert entry.details["hwid"] == "h-1"


async def test_6_5_delete_all_asks_confirmation(shop: Shop) -> None:
    """6.5: «Удалить все» — сначала подтверждение; удаление — только после «Да»."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json(hwid="h-1"), device_json(hwid="h-2")]}
    await shop.press(USER, AskRemoveAll(subscription_id=subscription.id).pack())
    confirm = shop.telegram.last()
    assert _text(confirm) == "Удалить все устройства? Их придётся подключить заново."
    assert _labels(confirm) == ["Да, удалить все", "Назад"]
    assert len(shop.panel.devices[7]) == 2

    await shop.press(USER, RemoveAll(subscription_id=subscription.id).pack())
    assert shop.panel.devices[7] == []


async def test_6_6_panel_down_queues_deletion(shop: Shop) -> None:
    """6.6: панель недоступна — удаление ставится в повтор, клиент видит, что устройство
    удалят, как только панель станет доступна; потом оно удаляется."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json(hwid="h-1")]}
    await add(shop.session, PanelOutage(started_at=datetime.now(UTC)))

    await shop.press(USER, RemoveAll(subscription_id=subscription.id).pack())

    assert "Устройство будет удалено, как только сервер станет доступен." in _text(
        shop.telegram.last()
    )
    [task] = await queued(shop.session, delete_devices)
    assert DeleteArgs.model_validate(task.args).hwid is None
    assert task.key == f"subscription:{subscription.id}"
    assert len(shop.panel.devices[7]) == 1

    with fake_runtime(panel=shop.panel):
        await run_queued(shop.session, delete_devices)
    assert shop.panel.devices[7] == []


async def test_6_7_devices_shown_per_subscription(shop: Shop) -> None:
    """6.7: у клиента несколько подписок — устройства каждой показываются отдельно."""
    first = await _subscription(shop)
    client = await shop.session.get(Client, first.client_id)
    assert client is not None
    second = await _subscription(shop, client=client, user_id=8, name="Ноутбук")
    shop.panel.devices = {
        7: [device_json(hwid="a", deviceModel="iPhone 15")],
        8: [device_json(hwid="b", userId=8, deviceModel="MacBook")],
    }
    assert "iPhone 15" in _text(await _open(shop, first))
    other = _text(await _open(shop, second))
    assert "Устройства подписки «Ноутбук»" in other
    assert "MacBook" in other
    assert "iPhone 15" not in other


async def test_6_2_card_shows_devices_used_of_limit(shop: Shop) -> None:
    """З3, 6.2: в карточке подписки — «Устройства: X из Y» и кнопка раздела."""
    subscription = await _subscription(shop)
    shop.panel.devices = {7: [device_json()]}
    await shop.press(USER, Card(subscription_id=subscription.id).pack())
    card = shop.telegram.last()
    assert isinstance(card, EditMessageText)
    assert card.rich_message is not None
    content = card.rich_message.model_dump_json()
    assert "Устройства" in content
    assert "1 из 3" in content
    assert "Устройства" in _labels(card)


async def test_5_8_device_list_refusal_does_not_break_reconcile(shop: Shop) -> None:
    """5.8, 4.8: панель отказала в списке устройств — сверка подписки всё равно проходит."""
    trial = await _subscription(shop)
    trial.is_trial = True
    shop.panel.users[7]["expireAt"] = "2027-01-01T00:00:00.000Z"
    shop.panel.devices = {}
    original = shop.panel.handle

    def refuse_devices(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/api/hwid/devices/"):
            return httpx2.Response(403, json={"message": "Forbidden"})
        return original(request)

    shop.panel.handle = refuse_devices  # type: ignore[method-assign]
    with fake_runtime(panel=shop.panel):
        args = ReconcileArgs(subscription_id=trial.id, source=Source.SYNC)
        await reconcile(shop.session, trial, args)
    assert trial.expires_at == datetime(2027, 1, 1, tzinfo=UTC)
