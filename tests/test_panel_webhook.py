"""Приём вебхуков панели (4.1, 4.2, 1.9)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.config import load_settings
from remnabay.crypto import SecretBox, generate_key
from remnabay.domain.panel_events import PanelEvent
from remnabay.journal import JournalEntry
from remnabay.panel import SIGNATURE_HEADER
from remnabay.panel._webhooks import sign
from remnabay.panel_sync import ReconcileArgs, Source, webhook_received
from remnabay.queue._models import QueueTask
from remnabay.shop_settings import PANEL_WEBHOOK_SECRET, ShopSettingValue, webhook_secret
from remnabay.web.app import create_app
from remnabay.web.deps import get_session
from remnabay.web.panel_webhook import PANEL_WEBHOOK_PATH
from tests.conftest import REQUIRED_ENV
from tests.domain_support import add, make_client, make_subscription
from tests.panel_support import user_json

BOX = SecretBox(REQUIRED_ENV["ENCRYPTION_KEY"])


@pytest.fixture
async def shop(
    valid_env: dict[str, str], db_session: AsyncSession
) -> AsyncIterator[httpx2.AsyncClient]:
    """Веб магазина: запросы идут в той же транзакции теста, что и проверки."""
    del valid_env
    app = create_app(load_settings(env_file=None))

    async def test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = test_session
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://shop") as client:
        yield client


def _user_event(user_id: int = 7, event: str = "user.modified") -> bytes:
    payload = {
        "scope": "user",
        "event": event,
        "timestamp": "2026-10-05T12:00:00.000Z",
        "data": user_json(id=user_id),
    }
    return json.dumps(payload).encode()


def _device_event(user_id: int = 7) -> bytes:
    device: dict[str, Any] = {
        "hwid": "hw-1",
        "userId": user_id,
        "platform": "Android",
        "osVersion": "15",
        "deviceModel": "Pixel",
        "userAgent": "Happ",
        "createdAt": "2026-10-05T12:00:00.000Z",
    }
    payload = {
        "scope": "user_hwid_devices",
        "event": "user_hwid_devices.added",
        "timestamp": "2026-10-05T12:00:00.000Z",
        "data": {"user": user_json(id=user_id), "hwidUserDevice": device},
    }
    return json.dumps(payload).encode()


async def _post(
    shop: httpx2.AsyncClient, body: bytes, signature: str | bytes | None
) -> httpx2.Response:
    headers = [(b"content-type", b"application/json")]
    if signature is not None:
        value = signature if isinstance(signature, bytes) else signature.encode()
        headers.append((SIGNATURE_HEADER.encode(), value))
    return await shop.post(PANEL_WEBHOOK_PATH, content=body, headers=headers)


async def _signed(shop: httpx2.AsyncClient, session: AsyncSession, body: bytes) -> httpx2.Response:
    return await _post(shop, body, sign(body, await webhook_secret(session, BOX)))


async def _reconciles(session: AsyncSession) -> list[tuple[str | None, ReconcileArgs]]:
    rows = await session.execute(
        select(QueueTask.key, QueueTask.args)
        .where(QueueTask.name == "panel.reconcile_subscription")
        .order_by(QueueTask.id)
    )
    return [(key, ReconcileArgs.model_validate(args)) for key, args in rows]


async def _journal_actions(session: AsyncSession) -> list[tuple[str, object]]:
    entries = await session.scalars(
        select(JournalEntry).where(JournalEntry.action.like("panel.%")).order_by(JournalEntry.id)
    )
    return [(entry.action, entry.details.get("reason")) for entry in entries]


async def _subscription_of_user(session: AsyncSession, panel_user_id: int = 7) -> int:
    client = await make_client(session)
    subscription = make_subscription(client, None, panel_user_id=panel_user_id)
    await add(session, subscription)
    return subscription.id


@pytest.mark.parametrize("signature", [None, "", "0" * 64, "not-hex", b"\xe9\xe9"])
async def test_4_1_event_without_valid_signature_is_rejected_and_journaled(
    shop: httpx2.AsyncClient, db_session: AsyncSession, signature: str | bytes | None
) -> None:
    """4.1: подписи нет или она неверна (в том числе с байтами вне ASCII) — событие
    отклоняется и записывается в журнал."""
    await _subscription_of_user(db_session)

    response = await _post(shop, _user_event(), signature)

    assert response.status_code == 401
    assert await _journal_actions(db_session) == [("panel.webhook_rejected", "signature")]
    assert await _reconciles(db_session) == []
    assert not await webhook_received(db_session)


async def test_4_1_event_signed_with_shop_secret_starts_reconcile(
    shop: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    """4.1, 1.9: событие с верной подписью принято; сверка подписки его пользователя
    поставлена после её операций; пункт чек-листа «Вебхук панели» выполнен."""
    subscription_id = await _subscription_of_user(db_session)

    response = await _signed(shop, db_session, _user_event())

    assert response.status_code == 200
    assert await _reconciles(db_session) == [
        (
            f"subscription:{subscription_id}",
            ReconcileArgs(
                subscription_id=subscription_id, source=Source.WEBHOOK, event="user.modified"
            ),
        )
    ]
    assert await webhook_received(db_session)


async def test_4_2_repeated_event_has_effect_once(
    shop: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    """4.2, сквозное правило 3: то же событие пришло повторно — эффект один раз, факт
    повтора — в журнале."""
    await _subscription_of_user(db_session)
    body = _user_event()

    first = await _signed(shop, db_session, body)
    second = await _signed(shop, db_session, body)

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await _reconciles(db_session)) == 1
    assert await _journal_actions(db_session) == [("panel.event_repeated", None)]


async def test_device_event_reconciles_subscription_of_its_user(
    shop: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    """Событие устройств тоже ведёт к сверке подписки пользователя."""
    subscription_id = await _subscription_of_user(db_session)

    response = await _signed(shop, db_session, _device_event())

    assert response.status_code == 200
    assert [args.subscription_id for _, args in await _reconciles(db_session)] == [subscription_id]


@pytest.mark.parametrize(
    "body",
    [
        _user_event(user_id=999),
        json.dumps({"scope": "node", "event": "node.connection_lost", "data": {}}).encode(),
    ],
    ids=["user-without-subscription", "node-event"],
)
async def test_event_without_subscription_is_accepted_and_ignored(
    shop: httpx2.AsyncClient, db_session: AsyncSession, body: bytes
) -> None:
    """Пользователь без подписки в магазине (создан в панели вручную) не усыновляется;
    события узлов магазину не нужны. Панель получает «принято» и не повторяет доставку."""
    response = await _signed(shop, db_session, body)

    assert response.status_code == 200
    assert await _reconciles(db_session) == []
    assert await db_session.scalar(select(func.count()).select_from(PanelEvent)) == 1


async def test_signed_but_unreadable_event_is_rejected(
    shop: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    """Подпись верна, но тело не разбирается — отказ с записью в журнале."""
    response = await _signed(shop, db_session, b"{not json")

    assert response.status_code == 400
    assert await _journal_actions(db_session) == [("panel.webhook_rejected", "unreadable")]


async def test_secret_unreadable_after_key_change_rejects_events(
    shop: httpx2.AsyncClient, db_session: AsyncSession
) -> None:
    """Секрет вебхука зашифрован другим ключом (сменили ENCRYPTION_KEY) — подпись проверить
    нечем: событие отклоняется, а не принимается без проверки."""
    other = SecretBox(generate_key()).encrypt("secret")
    await add(db_session, ShopSettingValue(key=PANEL_WEBHOOK_SECRET.key, value=other))
    body = _user_event()

    response = await _post(shop, body, sign(body, "secret"))

    assert response.status_code == 503
    assert await _journal_actions(db_session) == [("panel.webhook_rejected", "secret_unreadable")]
