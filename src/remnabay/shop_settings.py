"""Настройки магазина, которые оператор меняет в админке (04-operator-settings.md).

В `.env` — только секреты и инфраструктура (`remnabay.config`); всё остальное
хранится здесь. Значение по умолчанию живёт в коде: пока оператор настройку не
менял, строки в базе нет. Изменения действуют сразу — значение читается из базы
при каждом использовании.

Секреты, которые хранит магазин (секрет вебхука панели, позже — ключи
провайдеров), лежат в базе только зашифрованными ключом из `.env` (0046).
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, available_timezones

from pydantic import AfterValidator, Field, PositiveInt, TypeAdapter, ValidationError
from sqlalchemy import DateTime, ForeignKey, String, func, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.crypto import SecretBox
from remnabay.db import Base
from remnabay.journal import Actor, JsonValue, Outcome, Subject, record

logger = logging.getLogger(__name__)

JOURNAL_SUBJECT = "setting"
# Длина секрета вебхука: 32 случайных байта в base64url
_WEBHOOK_SECRET_BYTES = 32


class SettingError(Exception):
    """Значение настройки не проходит проверку."""


class ShopSettingValue(Base):
    """Значение настройки, изменённое оператором. Нет строки — действует значение по умолчанию."""

    __tablename__ = "shop_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


@dataclass(frozen=True)
class ShopSetting[T]:
    """Настройка: ключ, проверка значения (Pydantic) и значение по умолчанию."""

    key: str
    adapter: TypeAdapter[T]
    default: T

    def parse(self, raw: object) -> T:
        try:
            return self.adapter.validate_python(raw)
        except ValidationError as error:
            raise SettingError(f"Неверное значение настройки {self.key}: {error}") from error

    def dump(self, value: T) -> JsonValue:
        dumped: JsonValue = self.adapter.dump_python(value, mode="json")
        return dumped


@dataclass(frozen=True)
class SecretSetting:
    """Секрет, который хранит магазин: в базе — только зашифрованным."""

    key: str


type PositiveDuration = Annotated[timedelta, Field(gt=timedelta(0))]

_ATTEMPTS = TypeAdapter[int](PositiveInt)
_DURATION = TypeAdapter[timedelta](PositiveDuration)

# «Магазин»: язык бота по умолчанию (0018); тексты по умолчанию — пока только русские
SHOP_LANGUAGE = ShopSetting(
    "shop.default_language", TypeAdapter[str](Annotated[str, Field(pattern=r"^[a-z]{2}$")]), "ru"
)


def _known_time_zone(name: str) -> str:
    if name not in available_timezones():
        raise ValueError(f"Неизвестный часовой пояс {name}")
    return name


# «Магазин»: часовой пояс — периоды в админке (1.18) и запасной текст дат (0044)
SHOP_TIME_ZONE = ShopSetting(
    "shop.time_zone",
    TypeAdapter[str](Annotated[str, AfterValidator(_known_time_zone)]),
    "Europe/Moscow",
)
# «Оплата» → «Повтор операций»: прекращение через 1 час или после 20 попыток (4.14)
RETRY_MAX_ATTEMPTS = ShopSetting("retry.max_attempts", _ATTEMPTS, 20)
RETRY_WINDOW = ShopSetting("retry.window", _DURATION, timedelta(hours=1))
# «Панель»
PANEL_SYNC_INTERVAL = ShopSetting("panel.sync_interval", _DURATION, timedelta(minutes=15))
PANEL_OUTAGE_ALERT_AFTER = ShopSetting("panel.outage_alert_after", _DURATION, timedelta(minutes=5))
PANEL_WEBHOOK_SECRET = SecretSetting("panel.webhook_secret")
# Секрет вебхука Telegram: бот получает обновления только с ним (0051)
TELEGRAM_WEBHOOK_SECRET = SecretSetting("telegram.webhook_secret")


async def _stored(session: AsyncSession, key: str) -> tuple[JsonValue] | None:
    """Сохранённое значение — `(значение,)`, или `None`, если оператор настройку не менял.

    Читается колонка, а не объект: объект из карты сессии мог устареть после
    вставки или обновления мимо ORM.
    """
    row = (
        await session.execute(select(ShopSettingValue.value).where(ShopSettingValue.key == key))
    ).first()
    return None if row is None else (row[0],)


async def get_setting[T](session: AsyncSession, setting: ShopSetting[T]) -> T:
    """Текущее значение настройки.

    Испорченное значение в базе (например, после правки руками) не роняет магазин:
    действует значение по умолчанию, а в лог пишется предупреждение.
    """
    stored = await _stored(session, setting.key)
    if stored is None:
        return setting.default
    try:
        return setting.parse(stored[0])
    except SettingError:
        logger.warning(
            "Настройка %s испорчена в базе — действует значение по умолчанию", setting.key
        )
        return setting.default


async def shop_time_zone(session: AsyncSession) -> ZoneInfo:
    return ZoneInfo(await get_setting(session, SHOP_TIME_ZONE))


async def set_setting[T](
    session: AsyncSession, setting: ShopSetting[T], value: T, *, member_id: int
) -> None:
    """Меняет настройку от имени участника команды и пишет изменение в журнал (4.25)."""
    new = setting.parse(value)
    old = await get_setting(session, setting)
    await session.execute(
        insert(ShopSettingValue)
        .values(key=setting.key, value=setting.dump(new), updated_by_id=member_id)
        .on_conflict_do_update(
            index_elements=[ShopSettingValue.key],
            set_={"value": setting.dump(new), "updated_by_id": member_id, "updated_at": func.now()},
        )
    )
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="setting.changed",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, setting.key),
        details={"key": setting.key, "old": setting.dump(old), "new": setting.dump(new)},
    )


async def webhook_secret(session: AsyncSession, box: SecretBox) -> str:
    """Секрет вебхука панели (1.9). При первом обращении магазин генерирует его сам."""
    return await generated_secret(session, box, PANEL_WEBHOOK_SECRET)


async def generated_secret(session: AsyncSession, box: SecretBox, setting: SecretSetting) -> str:
    """Секрет, который магазин генерирует сам при первом обращении.

    Одновременные первые обращения получают один и тот же секрет: вставка без
    перезаписи. Не расшифровывается (сменили ENCRYPTION_KEY) — `SecretDecryptionError`.
    Символы — только A–Z, a–z, 0–9, `_` и `-`: такой секрет принимает и Telegram.
    """
    key = setting.key
    generated = await session.scalar(
        insert(ShopSettingValue)
        .values(key=key, value=box.encrypt(secrets.token_urlsafe(_WEBHOOK_SECRET_BYTES)))
        .on_conflict_do_nothing()
        .returning(ShopSettingValue.key)
    )
    if generated is not None:
        await record(
            session,
            actor=Actor.SYSTEM,
            action="setting.generated",
            outcome=Outcome.SUCCESS,
            subject=Subject(JOURNAL_SUBJECT, key),
        )
    stored = await _stored(session, key)
    if stored is None or not isinstance(stored[0], str):
        raise SettingError(f"Секрет {key} не сохранён")
    return box.decrypt(stored[0])


async def claim_interval(
    session: AsyncSession, key: str, *, now: datetime, every: timedelta
) -> bool:
    """Отметка «не чаще раза в `every`»: `True` — если прошлая была раньше окна
    (или её не было), и тогда отметка ставится на `now`. Атомарно: из двух
    одновременных вызовов отметку получает один."""
    stamp: JsonValue = now.isoformat()
    claimed = await session.scalar(
        insert(ShopSettingValue)
        .values(key=key, value=stamp)
        .on_conflict_do_update(
            index_elements=[ShopSettingValue.key],
            set_={"value": stamp, "updated_at": func.now()},
            # Значение — строка JSON: #>> '{}' достаёт её как текст
            where=ShopSettingValue.value.op("#>>")(literal_column("'{}'::text[]")).cast(
                DateTime(timezone=True)
            )
            <= now - every,
        )
        .returning(ShopSettingValue.key)
    )
    return claimed is not None
