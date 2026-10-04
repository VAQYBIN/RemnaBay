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
