"""Подключённые провайдеры: ключи оператора и доступность для клиента (3.33, 0046, 0049).

Ключи провайдера оператор вводит в админке («Настройки → Оплата»); в базе они
лежат только зашифрованными ключом из `.env`. Провайдер доступен клиенту, если
ключи расшифровываются и провайдер принимает счёт в валюте учёта магазина.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox, SecretDecryptionError
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.payments._provider import PaymentProvider, ProviderKind
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import ShopSettingValue, claim_interval, get_setting

logger = logging.getLogger(__name__)

PROVIDER_SUBJECT = "payment_provider"
# Ошибка расшифровки ключей — в журнал (0049), но не на каждое обращение
_KEYS_LOST_JOURNAL_EVERY = timedelta(hours=1)


def _credentials_key(code: str) -> str:
    return f"payments.{code}.credentials"


class ConnectionState(StrEnum):
    NOT_CONNECTED = "not_connected"
    READY = "ready"
    # Ключи не расшифровываются — сменили ENCRYPTION_KEY: «Ключи нужно ввести заново» (0049)
    KEYS_LOST = "keys_lost"
    # Провайдер не принимает валюту учёта — недоступен для выбора (3.33)
    CURRENCY_UNSUPPORTED = "currency_unsupported"


@dataclass(frozen=True)
class Connection:
    kind: ProviderKind
    state: ConnectionState
    provider: PaymentProvider | None

    @property
    def available(self) -> bool:
        """Клиент может платить через этого провайдера."""
        return self.state == ConnectionState.READY


class ProviderKeysError(Exception):
    """Ключи не подходят по формату."""


class Providers:
    """Виды провайдеров, которые знает магазин, и их подключения с ключами оператора."""

    def __init__(self, box: SecretBox, kinds: Sequence[ProviderKind]) -> None:
        self._box = box
        self._kinds = {kind.code: kind for kind in kinds}

    @property
    def kinds(self) -> list[ProviderKind]:
        return list(self._kinds.values())

    def kind(self, code: str) -> ProviderKind | None:
        return self._kinds.get(code)

    async def connection(self, session: AsyncSession, kind: ProviderKind) -> Connection:
        stored = await session.scalar(
            select(ShopSettingValue.value).where(
                ShopSettingValue.key == _credentials_key(kind.code)
            )
        )
        if not isinstance(stored, str):
            return Connection(kind, ConnectionState.NOT_CONNECTED, None)
        try:
            raw = self._box.decrypt(stored)
            credentials = kind.credentials_model.model_validate(json.loads(raw))
        except SecretDecryptionError, ValidationError, ValueError:
            await self._journal_keys_lost(session, kind)
            return Connection(kind, ConnectionState.KEYS_LOST, None)
        provider = kind.connect(credentials)
        if await get_setting(session, SHOP_CURRENCY) not in kind.currencies:
            return Connection(kind, ConnectionState.CURRENCY_UNSUPPORTED, provider)
        return Connection(kind, ConnectionState.READY, provider)

    async def connections(self, session: AsyncSession) -> list[Connection]:
        return [await self.connection(session, kind) for kind in self._kinds.values()]

    async def available(self, session: AsyncSession) -> list[PaymentProvider]:
        """Провайдеры, через которых клиент может платить сейчас (1.14, 3.33)."""
        return [
            connection.provider
            for connection in await self.connections(session)
            if connection.available and connection.provider is not None
        ]

    async def get(self, session: AsyncSession, code: str) -> PaymentProvider | None:
        """Провайдер по коду — для опроса и вебхука уже созданных счетов. Валюта не
        проверяется: счёт уже выставлен, и его оплату нужно принять (0008)."""
        kind = self._kinds.get(code)
        if kind is None:
            return None
        return (await self.connection(session, kind)).provider

    def parse_credentials(self, kind: ProviderKind, raw: dict[str, str]) -> BaseModel:
        try:
            return kind.credentials_model.model_validate(raw)
        except ValidationError as error:
            raise ProviderKeysError("Ключи провайдера заполнены неверно") from error

    async def save(
        self,
        session: AsyncSession,
        kind: ProviderKind,
        credentials: BaseModel,
        *,
        member_id: int,
    ) -> None:
        """Сохраняет ключи (уже проверенные у провайдера) и пишет факт в журнал без значений."""
        stored = self._box.encrypt(credentials.model_dump_json())
        key = _credentials_key(kind.code)
        await session.execute(
            insert(ShopSettingValue)
            .values(key=key, value=stored, updated_by_id=member_id)
            .on_conflict_do_update(
                index_elements=[ShopSettingValue.key],
                set_={"value": stored, "updated_by_id": member_id, "updated_at": func.now()},
            )
        )
        await record(
            session,
            actor=Actor.team_member(member_id),
            action="payments.provider_connected",
            outcome=Outcome.SUCCESS,
            subject=Subject(PROVIDER_SUBJECT, kind.code),
        )

    async def disconnect(
        self, session: AsyncSession, kind: ProviderKind, *, member_id: int
    ) -> None:
        await session.execute(
            delete(ShopSettingValue).where(ShopSettingValue.key == _credentials_key(kind.code))
        )
        await record(
            session,
            actor=Actor.team_member(member_id),
            action="payments.provider_disconnected",
            outcome=Outcome.SUCCESS,
            subject=Subject(PROVIDER_SUBJECT, kind.code),
        )

    async def _journal_keys_lost(self, session: AsyncSession, kind: ProviderKind) -> None:
        logger.warning(
            "Ключи провайдера %s не расшифровываются ключом из ENCRYPTION_KEY", kind.code
        )
        mark = f"payments.{kind.code}.keys_lost_journaled_at"
        if await claim_interval(
            session, mark, now=datetime.now(UTC), every=_KEYS_LOST_JOURNAL_EVERY
        ):
            await record(
                session,
                actor=Actor.SYSTEM,
                action="payments.provider_keys_lost",
                outcome=Outcome.FAILURE,
                subject=Subject(PROVIDER_SUBJECT, kind.code),
            )
