"""Журнал действий: критерии 4.25 и 4.26."""

from datetime import datetime

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.journal import Actor, ActorType, JournalEntry, Outcome, Subject, entries_for, record


async def test_4_25_entry_stores_who_what_when_subject_and_outcome(
    db_session: AsyncSession,
) -> None:
    """4.25: запись хранит кто, что, когда, над чем и с каким результатом."""
    payment = Subject("payment", 17)

    await record(
        db_session,
        actor=Actor.team_member(5),
        action="payment.resolved_manually",
        subject=payment,
        outcome=Outcome.SUCCESS,
        details={"comment": "клиент написал в поддержку"},
    )
    await db_session.commit()

    [entry] = await entries_for(db_session, payment)
    assert entry.actor == Actor.team_member(5)
    assert entry.action == "payment.resolved_manually"
    assert isinstance(entry.occurred_at, datetime)
    assert entry.occurred_at.tzinfo is not None
    assert entry.subject == payment
    assert entry.outcome is Outcome.SUCCESS
    assert entry.details == {"comment": "клиент написал в поддержку"}


@pytest.mark.parametrize(
    ("actor", "actor_type", "actor_ref"),
    [
        (Actor.team_member(5), ActorType.TEAM_MEMBER, "5"),
        (Actor.client(9), ActorType.CLIENT, "9"),
        (Actor.SYSTEM, ActorType.SYSTEM, None),
        (Actor.PANEL, ActorType.PANEL, None),
        (Actor.provider("yookassa"), ActorType.PROVIDER, "yookassa"),
    ],
)
async def test_4_25_every_kind_of_actor_is_recorded(
    db_session: AsyncSession, actor: Actor, actor_type: ActorType, actor_ref: str | None
) -> None:
    """4.25: «кто» — участник команды, клиент, система, панель или провайдер."""
    subject = Subject("subscription", 1)

    await record(
        db_session, actor=actor, action="test.event", subject=subject, outcome=Outcome.SUCCESS
    )

    [entry] = await entries_for(db_session, subject)
    assert entry.actor_type is actor_type
    assert entry.actor_ref == actor_ref
    assert entry.actor == actor


async def test_4_25_failure_and_event_without_subject(db_session: AsyncSession) -> None:
    """4.25: неуспешный результат и событие без объекта (например, отклонённый вебхук, 4.1)."""
    entry = await record(
        db_session,
        actor=Actor.PANEL,
        action="panel_webhook.rejected",
        outcome=Outcome.FAILURE,
        details={"reason": "invalid_signature"},
    )

    assert entry.id is not None
    assert entry.subject is None
    assert entry.outcome is Outcome.FAILURE


async def test_4_25_entries_for_subject_in_order_and_only_its_own(db_session: AsyncSession) -> None:
    """4.25: история объекта — только его записи, в порядке появления."""
    payment = Subject("payment", 1)
    other = Subject("payment", 2)
    for action in ("payment.created", "payment.paid", "payment.applied"):
        await record(
            db_session, actor=Actor.SYSTEM, action=action, subject=payment, outcome=Outcome.SUCCESS
        )
    await record(
        db_session,
        actor=Actor.SYSTEM,
        action="payment.created",
        subject=other,
        outcome=Outcome.SUCCESS,
    )

    entries = await entries_for(db_session, payment)

    assert [e.action for e in entries] == ["payment.created", "payment.paid", "payment.applied"]


async def test_4_25_entry_belongs_to_callers_transaction(db_session: AsyncSession) -> None:
    """4.25: запись делается в транзакции изменения — откат изменения откатывает и запись."""
    subject = Subject("subscription", 3)

    savepoint = await db_session.begin_nested()
    await record(
        db_session,
        actor=Actor.SYSTEM,
        action="test.event",
        subject=subject,
        outcome=Outcome.SUCCESS,
    )
    await savepoint.rollback()

    assert await entries_for(db_session, subject) == []


def test_4_25_actor_requires_reference_only_where_it_names_someone() -> None:
    """4.25: участник команды, клиент и провайдер — с уточнением «кто именно», остальные — без."""
    with pytest.raises(ValueError, match="нужен"):
        Actor(ActorType.CLIENT)
    with pytest.raises(ValueError, match="не нужен"):
        Actor(ActorType.SYSTEM, "1")


async def test_4_25_database_rejects_entry_without_actor_reference(
    db_session: AsyncSession,
) -> None:
    """4.25: база сама не даёт записать «клиента» без указания, какого."""
    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "INSERT INTO journal_entries (actor_type, action, outcome, details)"
                " VALUES ('client', 'test.event', 'success', '{}')"
            )
        )


async def _one_entry(session: AsyncSession) -> JournalEntry:
    entry = await record(
        session,
        actor=Actor.SYSTEM,
        action="test.event",
        subject=Subject("payment", 1),
        outcome=Outcome.SUCCESS,
    )
    await session.flush()
    return entry


async def test_4_26_entry_cannot_be_updated(db_session: AsyncSession) -> None:
    """4.26: изменить запись журнала нельзя — база отклоняет UPDATE."""
    entry = await _one_entry(db_session)

    with pytest.raises(DBAPIError, match="нельзя изменить или удалить"):
        await db_session.execute(
            update(JournalEntry).where(JournalEntry.id == entry.id).values(outcome=Outcome.FAILURE)
        )


async def test_4_26_entry_cannot_be_deleted(db_session: AsyncSession) -> None:
    """4.26: удалить запись журнала нельзя — база отклоняет DELETE."""
    entry = await _one_entry(db_session)

    with pytest.raises(DBAPIError, match="нельзя изменить или удалить"):
        await db_session.execute(delete(JournalEntry).where(JournalEntry.id == entry.id))


async def test_4_26_journal_cannot_be_truncated(db_session: AsyncSession) -> None:
    """4.26: очистить журнал целиком нельзя — база отклоняет TRUNCATE."""
    await _one_entry(db_session)

    with pytest.raises(DBAPIError, match="нельзя изменить или удалить"):
        await db_session.execute(text("TRUNCATE journal_entries"))
