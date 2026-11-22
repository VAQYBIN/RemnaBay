"""Команда оператора: участники, сессии админки, приглашения."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.domain._types import CreatedAt


class TeamRole(StrEnum):
    """Две фиксированные роли (0004)."""

    OWNER = "owner"
    ASSISTANT = "assistant"


class TeamMember(Base):
    """Участник команды (1.3). Входит в админку через Telegram, без паролей.

    Строка не удаляется при отзыве доступа: журнал ссылается на участника (4.25).
    """

    __tablename__ = "team_members"
    __table_args__ = (
        # Один Telegram-аккаунт — один действующий участник
        Index(
            "uq_team_members_active_telegram_id",
            "telegram_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[TeamRole] = mapped_column(str_enum_type(TeamRole, "team_role"))
    # Участник команды может быть и клиентом — это разные вещи, привязанные к одному человеку
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[CreatedAt]
    # Отзыв доступа: сессии завершаются сразу
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminSession(Base):
    """Сессия веб-админки. Хранится хэш токена, а не сам токен."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    team_member_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"), index=True)
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invitation(Base):
    """Одноразовая ссылка для вступления в команду (данные — MVP, функция — v1)."""

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    role: Mapped[TeamRole] = mapped_column(str_enum_type(TeamRole, "team_role"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("team_members.id"))
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))


class LoginMethod(StrEnum):
    """Как участник входит в админку (1.4, решение 0051)."""

    # Кнопка на странице входа → подтверждение в боте
    BOT_CONFIRM = "bot_confirm"
    # Кнопка login_url на /admin — до решения 0053; новых таких запросов нет
    LOGIN_URL = "login_url"


class LoginStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    # Сессия выдана: подтверждение одноразовое (1.5)
    USED = "used"


class LoginRequest(Base):
    """Попытка входа в админку. Одноразовая и действует ограниченное время (1.5).

    Для входа по кнопке на странице: токен ссылки на бот и отдельный ключ опроса
    браузера — оба хранятся хэшами; код показывается и на странице, и в боте.
    Для входа по `login_url` — хэш подписи Telegram: те же данные второй раз не
    принимаются.
    """

    __tablename__ = "login_requests"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    method: Mapped[LoginMethod] = mapped_column(str_enum_type(LoginMethod, "login_method"))
    status: Mapped[LoginStatus] = mapped_column(str_enum_type(LoginStatus, "login_status"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    poll_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)
    code: Mapped[str | None] = mapped_column(String(8))
    # Кто открыл ссылку в боте; подтвердить может только он
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    team_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
