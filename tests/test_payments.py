"""Платёж «оплачен, не применён» на стороне сервера (4.11, 4.19–4.24, 3.34, 0008)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from remnabay.attention import attention
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import Subscription
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import ActorType, entries_for
from remnabay.messaging import SendArgs
from remnabay.panel import PanelUnavailableError
from remnabay.payments import (
    Applied,
    PaymentActionError,
    apply_as_new_subscription,
    apply_payment,
    bind_unknown_payment,
    confirm_payment,
    payment_card,
    payment_subject,
    processing_notice,
    resolve_payment_manually,
    retry_payment,
)
from remnabay.queue import (
    AttemptResult,
    Ending,
    RetryPolicy,
    TaskContext,
    TaskRegistry,
    TaskStatus,
    Worker,
    WorkerConfig,
)
from remnabay.queue._models import QueueAttempt, QueueTask
from tests.domain_support import add, make_client, make_payment, make_subscription, make_tariff
from tests.panel_support import fake_runtime
from tests.queue_support import run_workers

PROVIDER = "yookassa"
END = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)


@dataclass
class FakeApplier:
    """Шаг применения для тестов: продлевает подписку или создаёт новую; может падать."""

    subscription_id: int = 0
    created: bool = False
    errors: list[Exception] = field(default_factory=list[Exception])
    calls: list[int] = field(default_factory=list[int])

    async def apply(self, session: AsyncSession, payment: Payment) -> Applied:
        self.calls.append(payment.id)
        if self.errors:
            raise self.errors.pop(0)
        return Applied(subscription_id=self.subscription_id, created=self.created)


async def _setup(session: AsyncSession, state: PaymentState = PaymentState.PENDING) -> Payment:
    """Клиент с подпиской и платёж за её продление; владелец — чтобы было кого уведомлять."""
    await add(session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
    client = await make_client(session, telegram_id=777)
    subscription = make_subscription(client, None, panel_user_id=7)
    subscription.expires_at = END
    await add(session, subscription)
    payment = make_payment(
        client,
        None,
        state=state,
        purpose=PaymentPurpose.RENEWAL,
        subscription_id=subscription.id,
    )
    await add(session, payment)
    return payment


async def _confirm(session: AsyncSession, payment: Payment) -> Payment:
    assert payment.provider_payment_id is not None
    return await confirm_payment(
        session,
        provider=PROVIDER,
        provider_payment_id=payment.provider_payment_id,
        amount=payment.amount,
        currency=payment.currency,
    )


async def _tasks(session: AsyncSession) -> list[tuple[str, str | None]]:
    rows = await session.execute(select(QueueTask.name, QueueTask.key).order_by(QueueTask.id))
    return [(name, key) for name, key in rows]


async def _messages(session: AsyncSession) -> list[SendArgs]:
    rows = await session.scalars(
        select(QueueTask.args).where(QueueTask.name == "messages.send").order_by(QueueTask.id)
    )
    return [SendArgs.model_validate(args) for args in rows]


async def _run(session: AsyncSession, applier: FakeApplier, payment: Payment) -> None:
    context = TaskContext(session=session, task_id=1, attempt_number=1)
    with fake_runtime(payments=applier):
        await apply_payment.run(context, {"payment_id": payment.id})
    await session.flush()


async def _actions(session: AsyncSession, payment: Payment) -> list[tuple[str, ActorType]]:
    entries = await entries_for(session, payment_subject(payment.id))
    return [(entry.action, entry.actor_type) for entry in entries]


async def _failed_apply_task(session: AsyncSession, payment: Payment) -> QueueTask:
    task = QueueTask(
        name="payments.apply",
        args={"payment_id": payment.id},
        key=f"subscription:{payment.subscription_id}",
        status=TaskStatus.FAILED,
    )
    await add(session, task)
    return task


# --- Подтверждение оплаты ---


@pytest.mark.parametrize(
    "state", [PaymentState.PENDING, PaymentState.EXPIRED, PaymentState.CANCELLED]
)
async def test_0008_confirmed_payment_is_paid_and_queued_for_applying(
    db_session: AsyncSession, state: PaymentState
) -> None:
    """0008, 3.9, 3.38, 3.34: провайдер подтвердил оплату — платёж «оплачен» (в том числе
    по истёкшему или отменённому счёту), применение поставлено после операций подписки,
    переход — в журнале от имени провайдера."""
    payment = await _setup(db_session, state)

    await _confirm(db_session, payment)

    assert payment.state == PaymentState.PAID
    assert payment.paid_at is not None
    assert await _tasks(db_session) == [
        ("payments.apply", f"subscription:{payment.subscription_id}"),
        ("payments.processing_notice", None),
    ]
    assert await _actions(db_session, payment) == [("payment.paid", ActorType.PROVIDER)]


async def test_rule_3_repeated_confirmation_does_nothing_twice(db_session: AsyncSession) -> None:
    """Сквозное правило 3: повторное подтверждение той же оплаты ничего не делает второй
    раз; факт повтора — в журнале."""
    payment = await _setup(db_session)

    await _confirm(db_session, payment)
    await _confirm(db_session, payment)

    assert len(await _tasks(db_session)) == 2
    assert [action for action, _ in await _actions(db_session, payment)] == [
        "payment.paid",
        "payment.confirmation_repeated",
    ]


async def test_4_23_unknown_payment_needs_attention_and_notifies_team(
    db_session: AsyncSession,
) -> None:
    """4.23: провайдер прислал оплату, которой магазин не знает, — «неизвестный платёж»
    в «Требуют внимания» и уведомление команде."""
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))

    payment = await confirm_payment(
        db_session,
        provider=PROVIDER,
        provider_payment_id="old-bot-invoice",
        amount=Decimal("199.00"),
        currency="RUB",
    )

    assert payment.is_unknown
    assert payment.state == PaymentState.PAID_NOT_APPLIED
    [notification] = await _messages(db_session)
    assert notification.text_key == "team.unknown_payment"
    assert notification.variables["amount"].startswith("199,00")
    summary = await attention(db_session, TaskRegistry(()))
    assert ([p.id for p in summary.payments], summary.count) == ([payment.id], 1)


# --- Применение ---


async def test_paid_payment_is_applied_once(db_session: AsyncSession) -> None:
    """3.34, правило 3: оплаченный платёж применяется и становится «применён»; повтор
    операции после этого шаг применения не вызывает."""
    payment = await _setup(db_session, PaymentState.PAID)
    assert payment.subscription_id is not None
    applier = FakeApplier(subscription_id=payment.subscription_id)

    await _run(db_session, applier, payment)
    await _run(db_session, applier, payment)

    assert payment.state == PaymentState.APPLIED
    assert payment.applied_at is not None
    assert applier.calls == [payment.id]
    assert await _actions(db_session, payment) == [("payment.applied", ActorType.SYSTEM)]


async def test_4_11_panel_unavailable_keeps_payment_paid(db_session: AsyncSession) -> None:
    """4.11: панель недоступна — платёж остаётся «оплачен» (операция ждёт панель)."""
    payment = await _setup(db_session, PaymentState.PAID)
    applier = FakeApplier(errors=[PanelUnavailableError("нет соединения")])

    with pytest.raises(PanelUnavailableError):
        await _run(db_session, applier, payment)

    assert payment.state == PaymentState.PAID


async def test_4_19_payment_not_applied_soon_tells_client_it_is_being_applied(
    db_session: AsyncSession,
) -> None:
    """4.19: платёж оплачен, но не применён сразу — клиент получает «Оплата получена,
    подписка создаётся»; применённому — не нужно."""
    payment = await _setup(db_session, PaymentState.PAID)
    context = TaskContext(session=db_session, task_id=1, attempt_number=1)

    await processing_notice.run(context, {"payment_id": payment.id})
    payment.state = PaymentState.APPLIED
    await processing_notice.run(context, {"payment_id": payment.id})

    assert [(m.chat_id, m.text_key) for m in await _messages(db_session)] == [
        (777, "event.payment_processing")
    ]


async def test_4_20_attempts_exhausted_payment_waits_for_team(db_session: AsyncSession) -> None:
    """4.20: автоматические попытки исчерпаны — «оплачен — не применён», платёж в
    «Требуют внимания» (счётчик на главной), команда получает уведомление."""
    payment = await _setup(db_session, PaymentState.PAID)
    await _failed_apply_task(db_session, payment)

    await apply_payment.ended(Ending.FAILED, db_session, 1, {"payment_id": payment.id})

    assert payment.state == PaymentState.PAID_NOT_APPLIED
    assert [m.text_key for m in await _messages(db_session)] == ["team.payment_not_applied"]
    summary = await attention(db_session, TaskRegistry((apply_payment,)))
    # Проваленная операция применения не считается второй раз: платёж показывается сам
    assert (summary.operations, [p.id for p in summary.payments], summary.count) == (
        [],
        [payment.id],
        1,
    )


async def test_4_21_payment_card_shows_client_conditions_attempts_and_error(
    db_session: AsyncSession,
) -> None:
    """4.21: карточка платежа — клиент, что должно было примениться, история попыток и
    текст ошибки."""
    payment = await _setup(db_session, PaymentState.PAID_NOT_APPLIED)
    task = await _failed_apply_task(db_session, payment)
    for number, error in ((1, "PanelRequestError: 500"), (2, "PanelRequestError: 400 A039")):
        await add(
            db_session,
            QueueAttempt(
                task_id=task.id,
                number=number,
                retry_round=1,
                result=AttemptResult.ERROR,
                error=error,
                finished_at=datetime.now(UTC),
            ),
        )

    card = await payment_card(db_session, payment.id)

    assert card.client is not None
    assert card.client.id == payment.client_id
    assert card.subscription is not None
    assert (card.payment.purpose, card.payment.tariff_snapshot) == (
        PaymentPurpose.RENEWAL,
        {"price": "199.00", "duration_days": 30},
    )
    assert [a.number for a in card.attempts] == [1, 2]
    assert card.last_error == "PanelRequestError: 400 A039"


# --- Действия команды (4.22–4.24) ---


async def _member(session: AsyncSession) -> int:
    member = TeamMember(telegram_id=2, role=TeamRole.ASSISTANT)
    await add(session, member)
    return member.id


async def test_4_22_retry_then_client_learns_result(db_session: AsyncSession) -> None:
    """4.22, 4.24: «Применить повторно» — платёж снова применяется новым кругом попыток;
    после применения клиент получает «Мы разобрались: подписка продлена до …»."""
    payment = await _setup(db_session, PaymentState.PAID_NOT_APPLIED)
    task = await _failed_apply_task(db_session, payment)
    member_id = await _member(db_session)
    assert payment.subscription_id is not None

    await retry_payment(db_session, payment.id, member_id=member_id)
    await db_session.refresh(task)
    assert (payment.state, task.status, task.retry_round) == (
        PaymentState.PAID,
        TaskStatus.PENDING,
        2,
    )

    await _run(db_session, FakeApplier(subscription_id=payment.subscription_id), payment)

    [message] = await _messages(db_session)
    assert (message.client_id, message.text_key) == (
        payment.client_id,
        "event.payment_resolved.renewed",
    )
    assert message.variables == {
        "subscription_name": "Основная",
        "end_date": "1 декабря 2026, 12:00 (МСК)",
    }
    assert ("payment.retried", ActorType.TEAM_MEMBER) in await _actions(db_session, payment)


async def test_4_22_apply_as_new_subscription(db_session: AsyncSession) -> None:
    """4.22, 4.24: «Применить иначе» — создать новую подписку (например, пользователя
    удалили в панели); после — «Мы разобрались: подписка создана»."""
    payment = await _setup(db_session, PaymentState.PAID_NOT_APPLIED)
    await _failed_apply_task(db_session, payment)
    old_subscription = payment.subscription_id
    client = await db_session.get(Client, payment.client_id)
    assert client is not None
    new_subscription = make_subscription(client, None, panel_user_id=8)
    await add(db_session, new_subscription)

    await apply_as_new_subscription(db_session, payment.id, member_id=await _member(db_session))
    assert (payment.purpose, payment.subscription_id) == (PaymentPurpose.PURCHASE, None)

    await _run(db_session, FakeApplier(subscription_id=new_subscription.id, created=True), payment)

    assert payment.subscription_id == new_subscription.id
    assert [m.text_key for m in await _messages(db_session)] == ["event.payment_resolved.created"]
    entry = (await entries_for(db_session, payment_subject(payment.id)))[0]
    assert entry.details["previous_subscription_id"] == old_subscription


async def test_4_22_resolve_manually_needs_comment_and_tells_client_neutrally(
    db_session: AsyncSession,
) -> None:
    """4.22, 4.24: «Отметить решённым вручную» — с обязательным комментарием, который видит
    только команда; платёж «решён вручную», операция подписки снята с ожидания, клиент
    получает нейтральное уведомление."""
    payment = await _setup(db_session, PaymentState.PAID_NOT_APPLIED)
    task = await _failed_apply_task(db_session, payment)
    member_id = await _member(db_session)

    with pytest.raises(PaymentActionError, match="комментарий"):
        await resolve_payment_manually(db_session, payment.id, member_id=member_id, comment=" ")
    await resolve_payment_manually(
        db_session, payment.id, member_id=member_id, comment="Продлил в панели руками"
    )

    await db_session.refresh(task)
    assert (payment.state, task.status) == (PaymentState.RESOLVED_MANUALLY, TaskStatus.RESOLVED)
    [message] = await _messages(db_session)
    assert (message.text_key, message.variables) == ("event.payment_resolved.manual", {})
    entries = await entries_for(db_session, payment_subject(payment.id))
    assert entries[-1].details["comment"] == "Продлил в панели руками"


async def test_4_23_unknown_payment_bound_to_client_is_applied(db_session: AsyncSession) -> None:
    """4.23: команда привязывает неизвестный платёж к клиенту и выбирает, что применить;
    продлить можно только подписку этого клиента."""
    payment = await _setup(db_session, PaymentState.PAID_NOT_APPLIED)
    assert payment.client_id is not None
    assert payment.subscription_id is not None
    client_id, subscription_id = payment.client_id, payment.subscription_id
    unknown = await confirm_payment(
        db_session,
        provider=PROVIDER,
        provider_payment_id="old-bot-invoice",
        amount=Decimal("199.00"),
        currency="RUB",
    )
    other_client = await make_client(db_session, telegram_id=888)
    member_id = await _member(db_session)
    tariff = make_tariff()
    await add(db_session, tariff)

    async def bind(client_id: int) -> None:
        await bind_unknown_payment(
            db_session,
            unknown.id,
            member_id=member_id,
            client_id=client_id,
            purpose=PaymentPurpose.RENEWAL,
            subscription_id=subscription_id,
            tariff_id=tariff.id,
            tariff_snapshot={"price": "199.00", "duration_days": 30},
        )

    with pytest.raises(PaymentActionError, match="этого клиента"):
        await bind(other_client.id)
    await bind(client_id)

    assert (unknown.client_id, unknown.state) == (client_id, PaymentState.PAID)
    assert ("payments.apply", f"subscription:{subscription_id}") in await _tasks(db_session)


async def test_team_actions_only_for_payment_waiting_for_team(db_session: AsyncSession) -> None:
    """Разбирать можно только платёж «оплачен — не применён»."""
    payment = await _setup(db_session, PaymentState.APPLIED)
    member_id = await _member(db_session)

    with pytest.raises(PaymentActionError):
        await retry_payment(db_session, payment.id, member_id=member_id)
    with pytest.raises(PaymentActionError):
        await resolve_payment_manually(db_session, payment.id, member_id=member_id, comment="—")


# --- Сквозной сценарий с воркером (4.11) ---


async def test_4_11_payment_paid_during_outage_is_applied_after_panel_returns(
    queue_engine: AsyncEngine,
) -> None:
    """4.11, 4.30: оплата пришла, пока панель недоступна, — платёж «оплачен», операция
    «ждёт панель» и не тратит попытки; панель вернулась — платёж применён сам."""
    async with AsyncSession(queue_engine, expire_on_commit=False) as session, session.begin():
        client = await make_client(session, telegram_id=99001)
        payment = make_payment(client, None, state=PaymentState.PENDING)
        await add(session, payment)
    applier = FakeApplier(
        subscription_id=0,
        created=True,
        errors=[PanelUnavailableError("нет соединения")] * 3,
    )
    worker = Worker(
        queue_engine,
        (apply_payment, processing_notice),
        policy=RetryPolicy(
            first_delay=timedelta(milliseconds=20),
            max_attempts=2,
            unavailable_recheck=timedelta(milliseconds=20),
        ),
        config=WorkerConfig(concurrency=1, poll_interval=timedelta(milliseconds=10)),
        unavailable=(PanelUnavailableError,),
    )
    subscription = make_subscription(client, None, panel_user_id=99001)
    try:
        async with AsyncSession(queue_engine, expire_on_commit=False) as session, session.begin():
            await add(session, subscription)
            applier.subscription_id = subscription.id
            await _confirm(session, payment)

        async def applied() -> bool:
            async with AsyncSession(queue_engine) as session:
                state = await session.scalar(select(Payment.state).where(Payment.id == payment.id))
            return state == PaymentState.APPLIED

        with fake_runtime(payments=applier):
            await run_workers([worker], applied)
        # Три попытки «панель недоступна» не съели лимит в две попытки
        assert len(applier.calls) == 4
    finally:
        async with AsyncSession(queue_engine) as session, session.begin():
            await session.execute(delete(Payment).where(Payment.id == payment.id))
            await session.execute(delete(Subscription).where(Subscription.id == subscription.id))
            await session.execute(
                delete(TelegramAccount).where(TelegramAccount.client_id == client.id)
            )
            await session.execute(delete(Client).where(Client.id == client.id))
