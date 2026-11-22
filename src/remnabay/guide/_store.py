"""Конфиги Subscription Page, сохранённые магазином (12.1, 12.7, 12.8, 12.16).

- Конфиг подписки назначает панель: через внешний сквад пользователя. Если сквада
  нет или у сквада нет конфига, панель отвечает пустым значением — это конфиг по
  умолчанию (12.1, решение 0043).
- Магазин хранит последний полученный конфиг и обновляет его раз в час и по кнопке
  «Обновить сейчас» (12.7). Панель недоступна — действует сохранённый (12.8).
- Вместе с конфигом магазин узнаёт, пускает ли панель браузер на страницу
  подписки: передаёт типичные заголовки браузера и сохраняет ответ (12.16).

Содержимое конфига хранится текстом, как его отдала панель: так сохраняется
порядок платформ и приложений.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay import runtime
from remnabay.db import Base
from remnabay.domain.subscriptions import Subscription
from remnabay.guide._config import ConfigError, GuideConfig, parse_config
from remnabay.journal import Actor, Outcome, record
from remnabay.panel import (
    PanelClient,
    PanelError,
    PanelRequestError,
    PanelResponseError,
    SubpageConfig,
    UserSubpageConfig,
)
from remnabay.queue import Periodic, TaskContext, task
from remnabay.shop_settings import ShopSetting, get_setting, remember_setting

logger = logging.getLogger(__name__)

# Конфиг по умолчанию: удалить его в панели нельзя (12.1, решение 0043)
DEFAULT_CONFIG_UUID = UUID(int=0)
REFRESH_EVERY = timedelta(hours=1)
# Сколько подписок попробовать, чтобы узнать у панели о браузере (12.16)
_BROWSER_CHECK_TRIES = 5
# Заголовки типичного браузера: по ним панель решает, пустит ли браузер (12.16)
BROWSER_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Пускает ли панель браузер на страницу подписки: последний ответ; `None` — ответа
# ещё не было, тогда считается, что пускает (12.16)
WEBPAGE_ALLOWED = ShopSetting("guide.webpage_allowed", TypeAdapter[bool | None](bool | None), None)


class StoredConfig(Base):
    """Последний полученный конфиг Subscription Page (12.7)."""

    __tablename__ = "subpage_configs"

    uuid: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # Содержимое как его отдала панель (JSON текстом)
    content: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


async def webpage_allowed(session: AsyncSession) -> bool:
    """Показывать ли «Открыть в браузере» (12.6, 12.16)."""
    return await get_setting(session, WEBPAGE_ALLOWED) is not False


async def _remember_webpage_allowed(session: AsyncSession, allowed: bool) -> None:
    """Последний ответ панели; в журнал — только его смена."""
    if await remember_setting(session, WEBPAGE_ALLOWED, allowed):
        await record(
            session,
            actor=Actor.PANEL,
            action="guide.webpage_allowed_changed",
            outcome=Outcome.SUCCESS,
            details={"allowed": allowed},
        )


async def remember_assignment(
    session: AsyncSession, subscription: Subscription, answer: UserSubpageConfig
) -> UUID:
    """Ответ панели о конфиге подписки и о браузере — запоминается (12.1, 12.16)."""
    config_uuid = answer.subpage_config_uuid or DEFAULT_CONFIG_UUID
    subscription.subpage_config_uuid = config_uuid
    await _remember_webpage_allowed(session, answer.webpage_allowed)
    return config_uuid


async def assign_config(
    session: AsyncSession, panel: PanelClient, subscription: Subscription
) -> UUID:
    """Спрашивает у панели конфиг подписки и пускает ли она браузер; запоминает ответ.

    Ошибки панели — у вызывающего: тогда действует последнее известное назначение.
    """
    answer = await panel.get_user_subpage_config(subscription.panel_short_uuid, BROWSER_HEADERS)
    return await remember_assignment(session, subscription, answer)


def known_config_uuid(subscription: Subscription) -> UUID:
    """Последнее известное назначение конфига подписки; не известно — по умолчанию."""
    return subscription.subpage_config_uuid or DEFAULT_CONFIG_UUID


async def store_config(session: AsyncSession, config_uuid: UUID, config: SubpageConfig) -> None:
    """Сохраняет полученный конфиг (12.7)."""
    content = json.dumps(config.config or {}, ensure_ascii=False)
    values = {"name": config.name[:256], "content": content, "fetched_at": datetime.now(UTC)}
    await session.execute(
        insert(StoredConfig)
        .values(uuid=config_uuid, **values)
        .on_conflict_do_update(index_elements=[StoredConfig.uuid], set_=values)
    )


async def fetch_config(session: AsyncSession, panel: PanelClient, config_uuid: UUID) -> None:
    """Получает конфиг у панели и сохраняет его (12.7). Ошибки панели — у вызывающего."""
    await store_config(session, config_uuid, await panel.get_subpage_config(config_uuid))


async def stored_config(session: AsyncSession, config_uuid: UUID) -> GuideConfig | None:
    """Сохранённый конфиг или `None`, если его ещё ни разу не получали (12.8).
    Испорченный конфиг (не разбирается) — как неполученный, причина — в логе."""
    stored = await session.get(StoredConfig, config_uuid)
    if stored is None:
        return None
    try:
        return parse_config(stored.content)
    except ConfigError:
        logger.exception("Конфиг Subscription Page %s не разобран", config_uuid)
        return None


@dataclass(frozen=True)
class RefreshResult:
    configs: int
    webpage_allowed: bool | None


async def refresh_configs(session: AsyncSession, panel: PanelClient) -> RefreshResult:
    """Обновляет все нужные конфиги: по умолчанию и назначенные подпискам (12.7), и
    ответ «пускает ли браузер» (12.16).

    Панель недоступна — `PanelUnavailableError` у вызывающего. Отказ по одному конфигу
    (например, его удалили в панели, а подписка ещё помнит старый) остальные не
    останавливает: подписка узнает новое назначение, когда клиент откроет инструкцию.
    """
    assigned = await session.scalars(
        select(Subscription.subpage_config_uuid)
        .where(Subscription.subpage_config_uuid.is_not(None), Subscription.deleted_at.is_(None))
        .distinct()
    )
    uuids = [DEFAULT_CONFIG_UUID]
    uuids += [uuid for uuid in assigned if uuid is not None and uuid != DEFAULT_CONFIG_UUID]
    fetched = 0
    for config_uuid in uuids:
        try:
            await fetch_config(session, panel, config_uuid)
        except PanelRequestError, PanelResponseError:
            if config_uuid == DEFAULT_CONFIG_UUID:
                raise
            logger.warning("Конфиг Subscription Page %s не получен", config_uuid, exc_info=True)
            continue
        fetched += 1
    # Ответ о браузере панель даёт по ссылке подписки: берём недавние действующие
    short_uuids = await session.scalars(
        select(Subscription.panel_short_uuid)
        .where(Subscription.deleted_at.is_(None))
        .order_by(Subscription.id.desc())
        .limit(_BROWSER_CHECK_TRIES)
    )
    for short_uuid in short_uuids:
        try:
            answer = await panel.get_user_subpage_config(short_uuid, BROWSER_HEADERS)
        except PanelRequestError:
            # Пользователя удалили в панели, сверка ещё не знает — следующая подписка
            continue
        await _remember_webpage_allowed(session, answer.webpage_allowed)
        break
    return RefreshResult(fetched, await get_setting(session, WEBPAGE_ALLOWED))


async def last_refreshed_at(session: AsyncSession) -> datetime | None:
    return await session.scalar(
        select(StoredConfig.fetched_at).where(StoredConfig.uuid == DEFAULT_CONFIG_UUID)
    )


class RefreshArgs(BaseModel):
    pass


@task("guide.refresh_configs", RefreshArgs, needs_attention=False)
async def refresh_configs_task(context: TaskContext, args: RefreshArgs) -> None:
    """Периодическое обновление конфигов (12.7). Панель недоступна — задача ждёт её;
    до тех пор действует сохранённый конфиг (12.8)."""
    del args
    try:
        await refresh_configs(context.session, runtime.current().panel)
    except PanelError:
        logger.warning("Конфиги Subscription Page не обновлены: панель ответила ошибкой")
        raise


REFRESH_CONFIGS = Periodic(refresh_configs_task, RefreshArgs(), REFRESH_EVERY)
