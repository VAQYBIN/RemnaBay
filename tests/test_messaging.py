"""Сообщения клиентам и команде (4.33, сквозное правило 4, 0018)."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.messaging import (
    AmbiguousDeliveryError,
    Button,
    ButtonArgs,
    DeliveryError,
    NotAcceptedError,
    OutgoingMessage,
    RecipientBlockedError,
    SendArgs,
    TelegramSender,
    UndeliverableError,
    notify_team,
    send_message,
    send_to_client,
)
from remnabay.queue import (
    AttemptResult,
    RejectedError,
    RetryPolicy,
    TaskContext,
    TaskStatus,
    Worker,
    WorkerConfig,
    attempts_of,
)
from remnabay.queue._models import QueueTask
from remnabay.shop_settings import claim_interval
from remnabay.texts import BotTextOverride
from remnabay.worker.main import (
    OPERATION_FAILED_EVERY,
    OPERATION_FAILED_MARK,
    notify_operation_failed,
)
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_client
from tests.panel_support import FakeSender, fake_runtime
from tests.queue_support import all_finished, run_workers, status_of

TOKEN = "123456:test-token"  # noqa: S105 — не настоящий токен
_MS = timedelta(milliseconds=1)


# --- Подставной Bot API: разбор ответов Telegram настоящим aiogram ---


@dataclass
class FakeBotApi:
    """Отвечает на sendMessage по очереди заготовленными ответами."""

    # Ответ — (статус, тело) или "slow": ответить позже таймаута отправителя
    replies: list[tuple[int, str] | str] = field(default_factory=list[tuple[int, str] | str])
    requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    async def handle(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.requests.append({key: str(value) for key, value in form.items()})
        reply = self.replies.pop(0) if self.replies else _ok()
        if isinstance(reply, str):
            await asyncio.sleep(5)
            reply = _ok()
        status, body = reply
        return web.Response(status=status, text=body, content_type="application/json")


def _ok() -> tuple[int, str]:
    message = {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"}, "text": "x"}
    return 200, json.dumps({"ok": True, "result": message})


def _error(status: int, description: str, **parameters: int) -> tuple[int, str]:
    body: dict[str, object] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return status, json.dumps(body)


@pytest.fixture
async def bot_api() -> AsyncIterator[tuple[FakeBotApi, str]]:
    api = FakeBotApi()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield api, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def _send_via(
    base: str, message: OutgoingMessage, wait: timedelta = timedelta(seconds=2)
) -> None:
    sender = TelegramSender(TOKEN, api_base=base, timeout=wait)
    try:
        await sender.send(42, message)
    finally:
        await sender.close()


async def test_message_with_copy_and_link_buttons_is_sent(
    bot_api: tuple[FakeBotApi, str],
) -> None:
    """Сообщение уходит с кнопками копирования и ссылки (С15: «Скопировать ссылку», 4.29)."""
    api, base = bot_api
    message = OutgoingMessage(
        "Ссылка обновлена",
        (Button("Скопировать ссылку", copy_text="https://sub/x"), Button("Сайт", url="https://s")),
    )

    await _send_via(base, message)

    sent = api.requests[0]
    assert (sent["chat_id"], sent["text"]) == ("42", "Ссылка обновлена")
    keyboard = json.loads(sent["reply_markup"])["inline_keyboard"]
    assert keyboard == [
        [{"text": "Скопировать ссылку", "copy_text": {"text": "https://sub/x"}}],
        [{"text": "Сайт", "url": "https://s"}],
    ]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (_error(403, "Forbidden: bot was blocked by the user"), RecipientBlockedError),
        (_error(429, "Too Many Requests: retry after 5", retry_after=5), NotAcceptedError),
        (_error(401, "Unauthorized"), NotAcceptedError),
        (_error(400, "Bad Request: chat not found"), UndeliverableError),
        (_error(500, "Internal Server Error"), AmbiguousDeliveryError),
        ((502, "<html>Bad Gateway</html>"), AmbiguousDeliveryError),
        ("slow", AmbiguousDeliveryError),
    ],
    ids=[
        "blocked",
        "too-many",
        "bad-token",
        "chat-not-found",
        "server-error",
        "gateway",
        "timeout",
    ],
)
async def test_4_33_telegram_answer_tells_whether_message_was_accepted(
    bot_api: tuple[FakeBotApi, str],
    reply: tuple[int, str] | str,
    expected: type[DeliveryError],
) -> None:
    """4.33: повторять можно, только если Telegram точно не принял сообщение; таймаут и
    ошибка сервера — неизвестно, принял ли: повтора нет."""
    api, base = bot_api
    api.replies.append(reply)

    with pytest.raises(expected):
        await _send_via(base, OutgoingMessage("x"), timedelta(milliseconds=300))


async def test_4_33_no_connection_means_not_accepted() -> None:
    """4.33: соединение не установлено — запрос до Telegram не дошёл, повтор безопасен."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(NotAcceptedError):
        await _send_via(f"http://127.0.0.1:{port}", OutgoingMessage("x"))


# --- Операция «отправить сообщение» ---


async def _run_send(
    session: AsyncSession,
    sender: FakeSender,
    args: SendArgs,
    previous: AttemptResult | None = None,
) -> None:
    context = TaskContext(session=session, task_id=1, attempt_number=1, previous_result=previous)
    with fake_runtime(sender):
        await send_message.run(context, args.model_dump(mode="json"))


async def _queued(session: AsyncSession) -> list[SendArgs]:
    """Поставленные в очередь сообщения, по порядку постановки."""
    rows = await session.scalars(select(QueueTask.args).order_by(QueueTask.id))
    return [SendArgs.model_validate(args) for args in rows]


async def _not_delivered(session: AsyncSession) -> list[dict[str, object]]:
    entries = await session.scalars(
        select(JournalEntry).where(
            JournalEntry.action == "message.not_delivered", journaled_in_test()
        )
    )
    return [dict(entry.details) for entry in entries]


async def test_0018_text_by_key_in_client_language_with_operator_override(
    db_session: AsyncSession,
) -> None:
    """0018: текст собирается по ключу при отправке: язык клиента (нет перевода — язык по
    умолчанию), правка оператора, переменные; подпись кнопки — тоже по ключу."""
    client = await make_client(db_session)
    client.language_code = "en"
    await add(
        db_session,
        BotTextOverride(
            key="event.link_revoked", language="ru", text="{subscription_name}: новая ссылка"
        ),
    )
    sender = FakeSender()
    args = SendArgs(
        chat_id=100,
        client_id=client.id,
        text_key="event.link_revoked",
        variables={"subscription_name": "Основная", "subscription_link": "https://sub/x"},
        buttons=[ButtonArgs(text_key="btn.copy_link", copy_text="https://sub/x")],
    )

    await _run_send(db_session, sender, args)

    assert sender.sent == [
        (
            100,
            OutgoingMessage(
                "Основная: новая ссылка",
                (Button("Скопировать ссылку", copy_text="https://sub/x"),),
            ),
        )
    ]


async def test_rule_4_blocked_bot_is_not_an_error(db_session: AsyncSession) -> None:
    """Сквозное правило 4, 11.9: клиент заблокировал бот — операция не проваливается,
    клиент получает пометку «бот заблокирован», недоставка — в журнале."""
    client = await make_client(db_session)
    sender = FakeSender(errors=[RecipientBlockedError("Forbidden: bot was blocked by the user")])

    await _run_send(
        db_session, sender, SendArgs(chat_id=100, client_id=client.id, text_key="btn.renew")
    )

    await db_session.refresh(client)
    assert client.bot_blocked_at is not None
    assert await _not_delivered(db_session) == [
        {"key": "btn.renew", "reason": "blocked", "error": "Forbidden: bot was blocked by the user"}
    ]


async def test_4_33_message_not_accepted_is_retried(db_session: AsyncSession) -> None:
    """4.33: Telegram точно не принял сообщение — операция просит повтор, он безопасен."""
    sender = FakeSender(errors=[NotAcceptedError("Too Many Requests")])

    with pytest.raises(RejectedError):
        await _run_send(db_session, sender, SendArgs(chat_id=100, text_key="btn.renew"))
    assert sender.sent == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AmbiguousDeliveryError("Request timeout error"), "ambiguous"),
        (UndeliverableError("Bad Request: chat not found"), "rejected"),
    ],
)
async def test_4_33_ambiguous_or_refused_message_is_not_retried(
    db_session: AsyncSession, error: DeliveryError, reason: str
) -> None:
    """4.33: при неоднозначной ошибке (таймаут) повтора нет — лучше потерять сообщение,
    чем отправить дважды; отказ по существу тоже не повторяется. Всё — в журнале."""
    sender = FakeSender(errors=[error])

    await _run_send(db_session, sender, SendArgs(chat_id=100, text_key="btn.renew"))

    assert [d["reason"] for d in await _not_delivered(db_session)] == [reason]


async def test_4_33_interrupted_attempt_is_not_repeated(db_session: AsyncSession) -> None:
    """4.33: прошлая попытка оборвалась посреди отправки (упал воркер) — сообщение могло
    уйти, поэтому второй раз оно не отправляется."""
    sender = FakeSender()

    await _run_send(
        db_session, sender, SendArgs(chat_id=100, text_key="btn.renew"), AttemptResult.ABORTED
    )

    assert sender.sent == []
    assert [d["reason"] for d in await _not_delivered(db_session)] == ["ambiguous"]


async def test_message_is_queued_in_callers_transaction(db_session: AsyncSession) -> None:
    """Сообщение ставится в очередь в транзакции действия: откатилось действие — не уйдёт
    и сообщение. Клиент без Telegram-аккаунта — недоставка в журнале."""
    client = await make_client(db_session, telegram_id=777)
    no_telegram = Client()
    await add(db_session, no_telegram)

    await send_to_client(db_session, client.id, "event.action_failed")
    await send_to_client(db_session, no_telegram.id, "event.action_failed")

    queued = await _queued(db_session)
    assert [(a.chat_id, a.client_id, a.text_key) for a in queued] == [
        (777, client.id, "event.action_failed")
    ]
    assert [d["reason"] for d in await _not_delivered(db_session)] == ["no_telegram"]


async def test_team_notification_goes_to_active_owners(db_session: AsyncSession) -> None:
    """Уведомления команде в MVP — владельцам (04-operator-settings): не помощникам и не
    тем, у кого доступ отозван."""
    await add(
        db_session,
        TeamMember(telegram_id=1, role=TeamRole.OWNER),
        TeamMember(telegram_id=2, role=TeamRole.OWNER),
        TeamMember(telegram_id=3, role=TeamRole.ASSISTANT),
    )
    revoked = TeamMember(telegram_id=4, role=TeamRole.OWNER)
    await add(db_session, revoked)
    revoked.revoked_at = revoked.created_at
    await db_session.flush()

    await notify_team(db_session, "team.operation_failed")

    queued = await _queued(db_session)
    assert sorted(a.chat_id for a in queued) == [1, 2]
    assert {a.text_key for a in queued} == {"team.operation_failed"}


async def test_4_31_failed_operation_notifies_team(db_session: AsyncSession) -> None:
    """4.31: операция без своих последствий провалилась — команда получает уведомление
    с числом ждущих разбора (0049)."""
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))

    await notify_operation_failed(db_session, 5, "trial.create")

    queued = await _queued(db_session)
    assert [(a.chat_id, a.text_key, a.variables) for a in queued] == [
        (1, "team.operation_failed", {"count": "0"})
    ]


async def test_0049_mass_failure_sends_one_notification(db_session: AsyncSession) -> None:
    """0049: при массовом провале — одно уведомление, а не по одному на операцию."""
    await add(db_session, TeamMember(telegram_id=1, role=TeamRole.OWNER))

    for task_id in range(5):
        await notify_operation_failed(db_session, task_id, "trial.create")

    assert len(await _queued(db_session)) == 1


async def test_0049_notification_window_is_ten_minutes(db_session: AsyncSession) -> None:
    """0049: следующее уведомление — не раньше чем через 10 минут после прошлого."""
    start = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    async def claim(at: datetime) -> bool:
        return await claim_interval(
            db_session, OPERATION_FAILED_MARK, now=at, every=OPERATION_FAILED_EVERY
        )

    assert await claim(start) is True
    assert await claim(start + timedelta(minutes=9, seconds=59)) is False
    assert await claim(start + timedelta(minutes=10)) is True
    assert await claim(start + timedelta(minutes=15)) is False


async def test_4_33_message_is_sent_once_after_refusals(queue_engine: AsyncEngine) -> None:
    """4.33: Telegram дважды не принял сообщение — очередь повторяет, и сообщение уходит
    ровно один раз."""
    sender = FakeSender(errors=[NotAcceptedError("429"), NotAcceptedError("429")])
    async with AsyncSession(queue_engine) as session, session.begin():
        task_id = await send_message.enqueue(
            session,
            SendArgs(chat_id=100, text_key="team.operation_failed", variables={"count": "1"}),
        )
    worker = Worker(
        queue_engine,
        (send_message,),
        policy=RetryPolicy(
            first_delay=_MS * 20, max_delay=_MS * 20, max_attempts=5, unavailable_recheck=_MS * 20
        ),
        config=WorkerConfig(concurrency=1, poll_interval=_MS * 10),
    )

    with fake_runtime(sender):
        await run_workers([worker], lambda: all_finished(queue_engine, [task_id]))

    assert await status_of(queue_engine, task_id) == TaskStatus.DONE
    assert len(sender.sent) == 1
    async with AsyncSession(queue_engine) as session:
        attempts = await attempts_of(session, task_id)
    assert [a.result for a in attempts] == [
        AttemptResult.REJECTED,
        AttemptResult.REJECTED,
        AttemptResult.DONE,
    ]
