"""Клиент и его способы входа."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, String, false
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base
from remnabay.domain._types import CreatedAt


class Client(Base):
    """Человек, который платит и управляет подписками. Не зависит от канала."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[CreatedAt]
    # Когда клиент впервые появился: открыл бот магазина или, для перенесённых,
    # старый бот («Дата появления» в промежуточном формате миграции)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда клиент впервые открыл бот магазина; у усыновлённого — пусто, пока не откроет
    bot_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Пометка «усыновлён» (10.2); перенесённые не входят в «новых клиентов» (1.17)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Пометка «перенесён из файла» миграции (9.7, 9.17, решение 0071): повторный импорт
    # такого клиента пропускает
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Что файл миграции сказал об оплатах в старом боте: да, нет или ничего (решение 0070)
    old_bot_paid: Mapped[bool | None] = mapped_column(Boolean)
    # Язык клиента для текстов бота (0018): из Telegram или из файла миграции
    language_code: Mapped[str | None] = mapped_column(String(16))
    # Пометка «бот заблокирован» (11.9): снимается, когда клиент снова открывает бот
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Клиент отключил рассылки сам (данные — MVP, функция — v1)
    broadcasts_disabled: Mapped[bool] = mapped_column(Boolean, server_default=false())


class TelegramAccount(Base):
    """Способ входа «Telegram-аккаунт» — единственный в MVP.

    Один Telegram-аккаунт — один клиент: по нему магазин узнаёт клиента.
    """

    __tablename__ = "telegram_accounts"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[CreatedAt]
