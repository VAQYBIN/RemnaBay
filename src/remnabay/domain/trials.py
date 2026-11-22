"""Триал и реестр устройств (блок 5, решение 0013)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base
from remnabay.domain._types import CreatedAt


class Trial(Base):
    """Факт использования триала. Триал принадлежит клиенту — один на клиента.

    Строка появляется, когда пользователь панели создан (5.6): отменённое создание
    триала не делает его использованным (4.32).
    """

    __tablename__ = "trials"

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    # Пусто, если факт перенесён из старого бота без подписки (9.17)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    imported: Mapped[bool] = mapped_column(Boolean, server_default=false())
    used_at: Mapped[CreatedAt]
    # Отзыв по устройству (v1); отозванный триал считается использованным
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)


class DeviceRecord(Base):
    """Факт подключения устройства к подписке (5.7).

    Не удаляется при удалении устройства клиентом или в панели (5.9): история нужна
    для проверки триала.
    """

    __tablename__ = "device_records"
    __table_args__ = (UniqueConstraint("subscription_id", "hwid"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    hwid: Mapped[str] = mapped_column(String(256), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
