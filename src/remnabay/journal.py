"""Журнал действий (4.25–4.26): кто, что, когда, над чем и с каким результатом.

Записи только добавляются. Изменить или удалить их нельзя: в модуле нет таких
операций, а база отклоняет UPDATE, DELETE и TRUNCATE таблицы журнала триггером
(см. миграцию журнала).

Запись делается в транзакции того изменения, о котором она рассказывает:
откатилось изменение — откатилась и запись.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    String,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type

# Значение в JSON: подробности события хранятся как есть
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


class ActorType(StrEnum):
    """Кто вызвал событие: роль команды, клиент или действующее лицо без доступа."""

    TEAM_MEMBER = "team_member"
    CLIENT = "client"
    SYSTEM = "system"
    PANEL = "panel"
    PROVIDER = "provider"


# Для этих действующих лиц нужно знать, кто именно: id участника, id клиента, код провайдера
_ACTORS_WITH_REF = frozenset({ActorType.TEAM_MEMBER, ActorType.CLIENT, ActorType.PROVIDER})
_ACTORS_WITH_REF_SQL = ", ".join(f"'{actor}'" for actor in sorted(_ACTORS_WITH_REF))


class Outcome(StrEnum):
    """С каким результатом завершилось действие."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class Actor:
    """Кто: участник команды, клиент, система, панель или платёжный провайдер."""

    type: ActorType
    ref: str | None = None

    SYSTEM: ClassVar[Actor]
    PANEL: ClassVar[Actor]

    def __post_init__(self) -> None:
        if self.type in _ACTORS_WITH_REF and not self.ref:
            raise ValueError(f"Для действующего лица {self.type} нужен идентификатор")
        if self.type not in _ACTORS_WITH_REF and self.ref is not None:
            raise ValueError(f"Для действующего лица {self.type} идентификатор не нужен")

    @classmethod
    def team_member(cls, member_id: int) -> Actor:
        return cls(ActorType.TEAM_MEMBER, str(member_id))

    @classmethod
    def client(cls, client_id: int) -> Actor:
        return cls(ActorType.CLIENT, str(client_id))

    @classmethod
    def provider(cls, code: str) -> Actor:
        return cls(ActorType.PROVIDER, code)


Actor.SYSTEM = Actor(ActorType.SYSTEM)
Actor.PANEL = Actor(ActorType.PANEL)


@dataclass(frozen=True, init=False)
class Subject:
    """Над чем: вид объекта (`payment`, `subscription`, …) и его идентификатор.

    Внешних ключей нет намеренно: запись журнала переживает объект, о котором она.
    """

    type: str
    id: str

    def __init__(self, type: str, id: int | str) -> None:
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "id", str(id))


class JournalEntry(Base):
    """Запись журнала. Только чтение после вставки (4.26)."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint(
            f"(actor_type IN ({_ACTORS_WITH_REF_SQL})) = (actor_ref IS NOT NULL)",
            name="actor_ref_matches_type",
        ),
        CheckConstraint("(subject_type IS NULL) = (subject_id IS NULL)", name="subject_complete"),
        CheckConstraint("action <> ''", name="action_not_empty"),
        Index("ix_journal_entries_subject", "subject_type", "subject_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Время самого действия, а не начала транзакции: в одной транзакции бывает несколько событий
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    actor_type: Mapped[ActorType] = mapped_column(str_enum_type(ActorType, "actor_type"))
    actor_ref: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(100))
    subject_type: Mapped[str | None] = mapped_column(String(50))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[Outcome] = mapped_column(str_enum_type(Outcome, "outcome"))
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default="{}")

    @property
    def actor(self) -> Actor:
        return Actor(self.actor_type, self.actor_ref)

    @property
    def subject(self) -> Subject | None:
        if self.subject_type is None or self.subject_id is None:
            return None
        return Subject(self.subject_type, self.subject_id)


async def record(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    outcome: Outcome,
    subject: Subject | None = None,
    details: dict[str, JsonValue] | None = None,
) -> JournalEntry:
    """Добавляет запись в журнал в текущей транзакции сессии."""
    entry = JournalEntry(
        actor_type=actor.type,
        actor_ref=actor.ref,
        action=action,
        subject_type=subject.type if subject else None,
        subject_id=subject.id if subject else None,
        outcome=outcome,
        details=details or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def entries_for(session: AsyncSession, subject: Subject) -> list[JournalEntry]:
    """Записи об объекте в порядке появления."""
    result = await session.scalars(
        select(JournalEntry)
        .where(JournalEntry.subject_type == subject.type, JournalEntry.subject_id == subject.id)
        .order_by(JournalEntry.id)
    )
    return list(result)
