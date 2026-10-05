"""Фабрики и помощники для тестов модели данных."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.db import Base
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.tariffs import Tariff, TariffType

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
GB = 1024**3


async def add(session: AsyncSession, *objects: Base) -> None:
    session.add_all(objects)
    await session.flush()


async def add_in_savepoint(session: AsyncSession, *objects: Base) -> None:
    async with session.begin_nested():
        session.add_all(objects)
        await session.flush()


async def assert_rejected(session: AsyncSession, *objects: Base) -> None:
    """База отклоняет запись; сессия остаётся пригодной для следующих шагов теста."""
    with pytest.raises(IntegrityError):
        await add_in_savepoint(session, *objects)


def make_tariff(**overrides: object) -> Tariff:
    values: dict[str, object] = {
        "name": "Месяц",
        "type": TariffType.TERM_UNLIMITED,
        "duration_days": 30,
        "price": Decimal("199.00"),
        "device_limit": 3,
        "squad_uuids": [uuid4()],
    }
    values.update(overrides)
    return Tariff(**values)


async def make_client(session: AsyncSession, telegram_id: int = 100) -> Client:
    client = Client()
    await add(session, client)
    await add(session, TelegramAccount(telegram_id=telegram_id, client_id=client.id))
    return client


def make_subscription(
    client: Client, tariff: Tariff | None, panel_user_id: int = 1
) -> Subscription:
    return Subscription(
        client_id=client.id,
        name="Основная",
        tariff_id=tariff.id if tariff else None,
        panel_user_id=panel_user_id,
        panel_username=f"user_{panel_user_id}",
        panel_short_uuid=f"short{panel_user_id}",
        subscription_url=f"https://sub.example.com/short{panel_user_id}",
    )


def make_payment(client: Client | None, tariff: Tariff | None, **overrides: object) -> Payment:
    values: dict[str, object] = {
        "client_id": client.id if client else None,
        "purpose": PaymentPurpose.PURCHASE,
        "state": PaymentState.PENDING,
        "tariff_id": tariff.id if tariff else None,
        "tariff_snapshot": {"price": "199.00", "duration_days": 30},
        "amount": Decimal("199.00"),
        "currency": "RUB",
        "provider": "yookassa",
        "provider_payment_id": str(uuid4()),
    }
    values.update(overrides)
    return Payment(**values)
