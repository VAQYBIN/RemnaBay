"""Рассылка (блок 11, сценарий «Рассылка», решение 0065): 11.1–11.13."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto, SendRichMessage
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.broadcasts import (
    BUY_CALLBACK,
    MAX_CAPTION_LENGTH,
    MAX_TEXT_LENGTH,
    RENEW_CALLBACK,
    BroadcastBusyError,
    BroadcastContent,
    ButtonStyle,
    FormatError,
    LinkButton,
    NoRecipientsError,
    Segment,
    SegmentKind,
    StepArgs,
    build_message,
    check_length,
    confirm,
    count_recipients,
    create_draft,
    parse,
    send_step,
    set_image,
    stop,
)
from remnabay.broadcasts import _sending as sending
from remnabay.domain.broadcasts import (
    Broadcast,
    BroadcastImage,
    BroadcastRecipient,
    BroadcastState,
    DeliveryState,
)
from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.subscriptions import PanelUserStatus
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry, Subject, entries_for
from remnabay.messaging import (
    MAIN_MENU_CALLBACK,
    AmbiguousDeliveryError,
    DeliveryError,
    NotAcceptedError,
    RecipientBlockedError,
    RecipientNotStartedError,
    SendArgs,
    TextEntity,
    UndeliverableError,
    load_texts,
    send_message,
)
from remnabay.queue import AttemptResult, Ending, RejectedError, TaskContext, TaskStatus
from remnabay.shop import SHOP_STATE, ShopState
from tests.bot_support import screen_buttons, telegram_user
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_client, make_subscription, make_tariff, put_setting
from tests.guide_support import queued
from tests.panel_support import FakeSender, fake_runtime
from tests.web_support import Shop, running_shop

NOW = datetime.now(UTC)
OWNER_TELEGRAM = 1
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Темп отправки (11.5) в тестах не нужен — проверяется отдельно."""
    monkeypatch.setattr(sending, "SEND_INTERVAL", timedelta())


async def _owner(session: AsyncSession) -> TeamMember:
    owner = await session.scalar(select(TeamMember).where(TeamMember.telegram_id == OWNER_TELEGRAM))
    if owner is None:
        owner = TeamMember(telegram_id=OWNER_TELEGRAM, role=TeamRole.OWNER)
        await add(session, owner)
    return owner


_next_panel_user = iter(range(10_000, 1_000_000))


async def _client_with(
    session: AsyncSession,
    telegram_id: int,
    *subscriptions: tuple[PanelUserStatus | None, timedelta | None],
    trial: bool = False,
    deleted: bool = False,
) -> Client:
    """Клиент с подписками: (статус в панели, через сколько истекает)."""
    client = await make_client(session, telegram_id=telegram_id)
    for status, expires_in in subscriptions:
        subscription = make_subscription(client, None, panel_user_id=next(_next_panel_user))
        subscription.panel_status = status
        subscription.expires_at = NOW + expires_in if expires_in is not None else None
        subscription.is_trial = trial
        subscription.deleted_at = NOW if deleted else None
        await add(session, subscription)
    return client


async def _draft(
    session: AsyncSession,
    text: str = "<b>Привет</b>",
    kind: SegmentKind = SegmentKind.ALL,
    **content: Any,
) -> int:
    owner = await _owner(session)
    return await create_draft(
        session,
        BroadcastContent(text=text, **content),
        Segment(kind=kind),
        member_id=owner.id,
    )


async def _run_steps(session: AsyncSession, *, previous: AttemptResult | None = None) -> int:
    """Выполняет шаги рассылки по цепочке, пока они ставятся (окружение — `fake_runtime`)."""
    ran = 0
    while rows := await queued(session, send_step):
        for task in rows:
            context = TaskContext(
                session=session, task_id=task.id, attempt_number=1, previous_result=previous
            )
            await send_step.run(context, task.args)
            task.status = TaskStatus.DONE
            ran += 1
        await session.flush()
    return ran


async def _recipients(session: AsyncSession, broadcast_id: int) -> dict[int, DeliveryState]:
    rows = await session.execute(
        select(BroadcastRecipient.client_id, BroadcastRecipient.state).where(
            BroadcastRecipient.broadcast_id == broadcast_id
        )
    )
    return {client_id: state for client_id, state in rows}


async def _state(session: AsyncSession, broadcast_id: int) -> BroadcastState:
    broadcast = await session.get_one(Broadcast, broadcast_id)
    await session.refresh(broadcast)
    return broadcast.state


async def _actions(session: AsyncSession, broadcast_id: int) -> list[str]:
    return [
        entry.action for entry in await entries_for(session, Subject("broadcast", broadcast_id))
    ]


# --- 11.1. Содержимое ---


def test_11_1_formatting_becomes_entities() -> None:
    """11.1: жирный, курсив, подчёркнутый, зачёркнутый, скрытый текст, ссылка, цитата,
    моноширинный — сущностями Telegram; смещения — в UTF-16."""
    text = parse(
        "🎉 <b>Жирный</b> <i>курсив</i> <u>подч</u> <s>зачёрк</s> "
        '<tg-spoiler>скрыт</tg-spoiler> <a href="https://example.com">ссылка</a>\n'
        "<blockquote>цитата</blockquote> <code>код</code> "
        '<pre><code class="language-python">x = 1</code></pre> &lt;3 &amp;'
    )
    assert text.text == ("🎉 Жирный курсив подч зачёрк скрыт ссылка\nцитата код x = 1 <3 &")
    kinds = {entity.type: entity for entity in text.entities}
    # Эмодзи — две единицы UTF-16 и пробел
    assert kinds["bold"] == TextEntity("bold", 3, 6)
    assert {"italic", "underline", "strikethrough", "spoiler", "blockquote", "code"} <= set(kinds)
    assert kinds["text_link"].url == "https://example.com"
    assert kinds["pre"].language == "python"
    assert [entity.type for entity in text.entities].count("code") == 1


def test_11_1_nested_formatting() -> None:
    """11.1: форматирование вкладывается — жирный курсив внутри цитаты."""
    text = parse("<blockquote><b><i>важно</i></b></blockquote>")
    assert {entity.type for entity in text.entities} == {"blockquote", "bold", "italic"}
    assert all(entity.offset == 0 and entity.length == 5 for entity in text.entities)


@pytest.mark.parametrize(
    ("markup", "problem"),
    [
        ("<h1>x</h1>", "не поддерживается"),
        ("<b>x", "Не закрыт"),
        ("<b><i>x</b></i>", "не на месте"),
        ('<a href="javascript:alert(1)">x</a>', "https://"),
        ('<span class="x">x</span>', "tg-spoiler"),
        ("a<br/>b", "Enter"),
    ],
)
def test_11_1_wrong_markup_is_explained(markup: str, problem: str) -> None:
    """11.1: ошибка разметки видна владельцу до отправки, а не в итогах рассылки."""
    with pytest.raises(FormatError, match=problem):
        parse(markup)


def test_11_1_text_and_caption_limits() -> None:
    """11.1: текст — до 4096 знаков, с изображением — до 1024; разметка не считается."""
    long = parse("<b>" + "я" * MAX_TEXT_LENGTH + "</b>")
    check_length(long, with_image=False)
    with pytest.raises(FormatError, match="4096"):
        check_length(parse("я" * (MAX_TEXT_LENGTH + 1)), with_image=False)
    check_length(parse("я" * MAX_CAPTION_LENGTH), with_image=True)
    with pytest.raises(FormatError, match="1024"):
        check_length(parse("я" * (MAX_CAPTION_LENGTH + 1)), with_image=True)


def test_11_1_up_to_three_links() -> None:
    """11.1: до трёх кнопок-ссылок; ссылка — https://, http:// или tg://."""
    link = LinkButton(text="Канал", url="https://t.me/channel")
    BroadcastContent(text="x", links=[link] * 3)
    with pytest.raises(ValidationError):
        BroadcastContent(text="x", links=[link] * 4)
    with pytest.raises(ValidationError):
        LinkButton(text="Канал", url="ftp://example.com")
    with pytest.raises(ValidationError):
        LinkButton(text="  ", url="https://example.com")


async def test_11_1_buttons_and_main_menu(db_session: AsyncSession) -> None:
    """11.1, решение 0069: «Купить», «Продлить», ссылки владельца; «Главное меню» —
    всегда и без цвета. Цвет каждой кнопки выбирает владелец; по умолчанию «Купить» и
    «Продлить» — зелёные, ссылки — без цвета."""
    texts = await load_texts(db_session)
    content = BroadcastContent(
        text="Новости",
        buy=True,
        links=[LinkButton(text="Канал", url="https://t.me/channel")],
    )
    message = build_message(content, texts, "ru", None)
    assert [(b.text, b.callback_data or b.url, b.style) for b in message.buttons] == [
        ("Купить подписку", BUY_CALLBACK, "success"),
        ("Канал", "https://t.me/channel", None),
        ("Главное меню", MAIN_MENU_CALLBACK, None),
    ]
    chosen = BroadcastContent(
        text="Новости",
        buy=True,
        buy_style=None,
        renew=True,
        renew_style=ButtonStyle.DANGER,
        links=[LinkButton(text="Канал", url="https://t.me/channel", style=ButtonStyle.PRIMARY)],
    )
    both = build_message(chosen, texts, "ru", None)
    assert [(b.callback_data or b.url, b.style) for b in both.buttons] == [
        (BUY_CALLBACK, None),
        (RENEW_CALLBACK, "danger"),
        ("https://t.me/channel", "primary"),
        (MAIN_MENU_CALLBACK, None),
    ]
    bare = build_message(BroadcastContent(text="x"), texts, "ru", None)
    assert [b.callback_data for b in bare.buttons] == [MAIN_MENU_CALLBACK]


# --- 11.2. Сегменты ---


async def test_11_2_segments(db_session: AsyncSession) -> None:
    """11.2, решение 0065: «с активной» — активна или исчерпала трафик, включая триал;
    «с истёкшей» — подписки есть, ни одной активной, хотя бы одна истекла; только
    отключённые в панели — лишь во «всех»; «бот заблокирован» в сегменты входит."""
    day = timedelta(days=1)
    before = {
        kind: await count_recipients(db_session, Segment(kind=kind), NOW) for kind in SegmentKind
    }
    await _client_with(db_session, 2001)  # без подписок
    await _client_with(db_session, 2002, (PanelUserStatus.ACTIVE, day))
    await _client_with(db_session, 2003, (PanelUserStatus.ACTIVE, day), trial=True)
    await _client_with(db_session, 2004, (PanelUserStatus.LIMITED, day))
    await _client_with(db_session, 2005, (PanelUserStatus.EXPIRED, -day))
    await _client_with(db_session, 2006, (PanelUserStatus.ACTIVE, -day))  # панель ещё не знает
    await _client_with(db_session, 2007, (PanelUserStatus.DISABLED, day))
    await _client_with(
        db_session, 2008, (PanelUserStatus.ACTIVE, -day), (PanelUserStatus.ACTIVE, day)
    )
    await _client_with(db_session, 2009, (PanelUserStatus.ACTIVE, day), deleted=True)
    blocked = await _client_with(db_session, 2010, (PanelUserStatus.EXPIRED, -day))
    blocked.bot_blocked_at = NOW
    # Клиент без Telegram-аккаунта: написать ему некуда
    await add(db_session, Client())
    await db_session.flush()

    after = {
        kind: await count_recipients(db_session, Segment(kind=kind), NOW) for kind in SegmentKind
    }
    assert after[SegmentKind.ALL] - before[SegmentKind.ALL] == 10
    # 2002, 2003 (триал), 2004 (трафик исчерпан), 2008 (одна из двух активна)
    assert after[SegmentKind.ACTIVE] - before[SegmentKind.ACTIVE] == 4
    # 2005, 2006, 2010 (бот заблокирован)
    assert after[SegmentKind.EXPIRED] - before[SegmentKind.EXPIRED] == 3


# --- 11.4–11.9, 11.11, 11.13. Подтверждение и отправка ---


async def test_11_4_recipients_fixed_at_confirmation(db_session: AsyncSession) -> None:
    """11.4, 11.11: состав получателей фиксируется при подтверждении; рассылка — в журнале."""
    first = await make_client(db_session, telegram_id=3001)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    count = await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    later = await make_client(db_session, telegram_id=3002)

    recipients = await _recipients(db_session, broadcast_id)
    assert len(recipients) == count
    assert first.id in recipients
    assert later.id not in recipients
    assert await _state(db_session, broadcast_id) == BroadcastState.SENDING
    assert await _actions(db_session, broadcast_id) == ["broadcast.started"]


async def test_11_5_11_6_11_8_each_recipient_once(db_session: AsyncSession) -> None:
    """11.5, 11.6, 11.8, 11.9, 11.11, решение 0069: каждый получает сообщение один раз;
    итог — доставлено, бот заблокирован, не открывали бот, ошибки; пометку «бот
    заблокирован» получает только заблокировавший."""
    clients = [await make_client(db_session, telegram_id=4000 + n) for n in range(5)]
    broadcast_id = await _draft(db_session, kind=SegmentKind.ALL)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    recipients = await _recipients(db_session, broadcast_id)
    ours = [client.id for client in clients]
    sender = FakeSender()
    with fake_runtime(sender):
        ran = await _run_steps_with_errors(db_session, sender, ours)
    assert ran == len(recipients)

    delivered = [chat for chat, _ in sender.sent]
    assert len(delivered) == len(set(delivered))
    final = await _recipients(db_session, broadcast_id)
    assert final[ours[0]] == DeliveryState.DELIVERED
    assert final[ours[1]] == DeliveryState.BOT_BLOCKED
    assert final[ours[2]] == DeliveryState.FAILED
    assert final[ours[3]] == DeliveryState.DELIVERED
    assert final[ours[4]] == DeliveryState.NOT_STARTED
    await db_session.refresh(clients[1])
    assert clients[1].bot_blocked_at is not None
    # Ни разу не открывал бот — не блокировка (решение 0069)
    await db_session.refresh(clients[4])
    assert clients[4].bot_blocked_at is None
    assert await _state(db_session, broadcast_id) == BroadcastState.FINISHED
    [*_, finished] = await entries_for(db_session, Subject("broadcast", broadcast_id))
    assert finished.action == "broadcast.finished"
    assert finished.details["bot_blocked"] == 1
    assert finished.details["failed"] == 1
    assert finished.details["not_started"] == 1

    # Повторное выполнение уже сделанного шага ничего не отправляет (11.6)
    with fake_runtime(sender):
        await send_step.enqueue(db_session, StepArgs(broadcast_id=broadcast_id, client_id=ours[0]))
        await _run_steps(db_session)
    assert len(sender.sent) == len(delivered)


async def _run_steps_with_errors(session: AsyncSession, sender: FakeSender, ours: list[int]) -> int:
    """Шаги по цепочке; второму, третьему и пятому из `ours` Telegram отвечает ошибкой."""
    ran = 0
    errors: dict[int, DeliveryError] = {
        ours[1]: RecipientBlockedError("blocked"),
        ours[2]: UndeliverableError("bad"),
        ours[4]: RecipientNotStartedError("can't initiate conversation"),
    }
    while rows := await queued(session, send_step):
        for task in rows:
            error = errors.get(int(str(task.args["client_id"])))
            if error is not None:
                sender.errors.append(error)
            context = TaskContext(session=session, task_id=task.id, attempt_number=1)
            await send_step.run(context, task.args)
            task.status = TaskStatus.DONE
            ran += 1
        await session.flush()
    return ran


async def test_11_6_interrupted_send_is_not_repeated(db_session: AsyncSession) -> None:
    """11.6: отправка оборвалась сбоем (попытка не завершилась) — сообщение могло уйти,
    повторно не отправляется; рассылка идёт дальше."""
    await make_client(db_session, telegram_id=5001)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    sender = FakeSender()
    with fake_runtime(sender):
        [task] = await queued(db_session, send_step)
        context = TaskContext(
            session=db_session,
            task_id=task.id,
            attempt_number=2,
            previous_result=AttemptResult.ABORTED,
        )
        await send_step.run(context, task.args)
    assert sender.sent == []
    first = int(str(task.args["client_id"]))
    assert (await _recipients(db_session, broadcast_id))[first] == DeliveryState.FAILED


async def test_11_6_not_accepted_is_retried_safely(db_session: AsyncSession) -> None:
    """11.5, 11.6: Telegram не принял сообщение (слишком много запросов) — шаг
    повторяется, и сообщение уходит один раз; неясный исход не повторяется."""
    await make_client(db_session, telegram_id=5101)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    sender = FakeSender(errors=[NotAcceptedError("429")])
    with fake_runtime(sender):
        [task] = await queued(db_session, send_step)
        context = TaskContext(session=db_session, task_id=task.id, attempt_number=1)
        with pytest.raises(RejectedError):
            await send_step.run(context, task.args)
        retry = TaskContext(
            session=db_session,
            task_id=task.id,
            attempt_number=2,
            previous_result=AttemptResult.REJECTED,
        )
        await send_step.run(retry, task.args)
    assert len(sender.sent) == 1

    # Неясный исход (таймаут) — ошибка получателя без повтора
    other = await _draft(db_session)
    await stop_all_sending(db_session)
    await confirm(db_session, other, member_id=owner.id, now=NOW)
    ambiguous = FakeSender(errors=[AmbiguousDeliveryError("timeout")])
    with fake_runtime(ambiguous):
        [task] = [t for t in await queued(db_session, send_step) if t.args["broadcast_id"] == other]
        context = TaskContext(session=db_session, task_id=task.id, attempt_number=1)
        await send_step.run(context, task.args)
    first = int(str(task.args["client_id"]))
    assert (await _recipients(db_session, other))[first] == DeliveryState.FAILED


async def stop_all_sending(session: AsyncSession) -> None:
    owner = await _owner(session)
    for broadcast_id in await session.scalars(
        select(Broadcast.id).where(Broadcast.state == BroadcastState.SENDING)
    ):
        await stop(session, broadcast_id, member_id=owner.id)


async def test_11_6_exhausted_step_moves_on(db_session: AsyncSession) -> None:
    """11.6: Telegram так и не принял сообщение — получатель с ошибкой, рассылка идёт
    дальше и заканчивается."""
    await make_client(db_session, telegram_id=5201)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    [task] = await queued(db_session, send_step)
    task.status = TaskStatus.CANCELLED
    await send_step.ended(Ending.FAILED, db_session, task.id, task.args)
    with fake_runtime():
        await _run_steps(db_session)
    first = int(str(task.args["client_id"]))
    assert (await _recipients(db_session, broadcast_id))[first] == DeliveryState.FAILED
    assert await _state(db_session, broadcast_id) == BroadcastState.FINISHED


async def test_11_7_stopped_broadcast_sends_nothing_more(db_session: AsyncSession) -> None:
    """11.7, 11.11: после остановки оставшимся получателям сообщение не отправляется."""
    for n in range(3):
        await make_client(db_session, telegram_id=6000 + n)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    sender = FakeSender()
    with fake_runtime(sender):
        [task] = await queued(db_session, send_step)
        await send_step.run(
            TaskContext(session=db_session, task_id=task.id, attempt_number=1), task.args
        )
        task.status = TaskStatus.DONE
        await stop(db_session, broadcast_id, member_id=owner.id)
        await _run_steps(db_session)
    assert len(sender.sent) == 1
    states = list((await _recipients(db_session, broadcast_id)).values())
    assert states.count(DeliveryState.DELIVERED) == 1
    assert DeliveryState.PENDING not in states
    assert states.count(DeliveryState.SKIPPED) == len(states) - 1
    assert await _state(db_session, broadcast_id) == BroadcastState.STOPPED
    assert await _actions(db_session, broadcast_id) == ["broadcast.started", "broadcast.stopped"]


async def test_11_13_one_broadcast_at_a_time(db_session: AsyncSession) -> None:
    """11.13: одновременно идёт одна рассылка."""
    await make_client(db_session, telegram_id=7001)
    owner = await _owner(db_session)
    await stop_all_sending(db_session)
    first = await _draft(db_session)
    second = await _draft(db_session)
    await confirm(db_session, first, member_id=owner.id, now=NOW)
    with pytest.raises(BroadcastBusyError):
        await confirm(db_session, second, member_id=owner.id, now=NOW)
    await stop(db_session, first, member_id=owner.id)
    await confirm(db_session, second, member_id=owner.id, now=NOW)


async def test_11_13_service_messages_go_first(db_session: AsyncSession) -> None:
    """11.13: пока служебное сообщение ждёт отправки, рассылка уступает ему очередь."""
    await make_client(db_session, telegram_id=7101)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    await send_message.enqueue(db_session, SendArgs(chat_id=7101, text_key="btn.main_menu"))
    sender = FakeSender()
    with fake_runtime(sender):
        [task] = await queued(db_session, send_step)
        await send_step.run(
            TaskContext(session=db_session, task_id=task.id, attempt_number=1), task.args
        )
        task.status = TaskStatus.DONE
    assert sender.sent == []
    [deferred] = await queued(db_session, send_step)
    assert deferred.args == task.args
    now = await db_session.scalar(select(func.clock_timestamp()))
    assert now is not None
    assert deferred.run_at > now


async def test_11_5_pace_limits_rate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11.5: сообщения рассылки идут не чаще `SEND_INTERVAL` — с учётом ограничений Telegram."""
    monkeypatch.setattr(sending, "SEND_INTERVAL", timedelta(milliseconds=60))
    for n in range(3):
        await make_client(db_session, telegram_id=7201 + n)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await stop_all_sending(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    with fake_runtime():
        started = datetime.now(UTC)
        ran = await _run_steps(db_session)
    assert datetime.now(UTC) - started >= timedelta(milliseconds=60) * (ran - 1)


async def test_11_1_image_uploaded_once(db_session: AsyncSession) -> None:
    """11.1: изображение загружается в Telegram один раз, дальше — по file_id; текст —
    подписью к нему."""
    for n in range(2):
        await make_client(db_session, telegram_id=7301 + n)
    broadcast_id = await _draft(db_session, text="Подпись")
    await set_image(db_session, broadcast_id, PNG)
    owner = await _owner(db_session)
    await stop_all_sending(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    sender = FakeSender()
    with fake_runtime(sender):
        await _run_steps(db_session)
    photos = [message.photo for _, message in sender.sent]
    assert photos[0] is not None
    assert photos[0].data == PNG
    assert all(photo is not None and photo.file_id == "file-1" for photo in photos[1:])
    assert sender.sent[0][1].text == "Подпись"
    image = await db_session.get_one(BroadcastImage, broadcast_id)
    assert image.telegram_file_id == "file-1"


async def test_11_4_empty_segment_is_not_sent(db_session: AsyncSession) -> None:
    """11.4: в сегменте нет получателей — рассылка не начинается."""
    broadcast_id = await _draft(db_session, kind=SegmentKind.EXPIRED)
    owner = await _owner(db_session)
    if await count_recipients(db_session, Segment(kind=SegmentKind.EXPIRED), NOW):
        pytest.skip("В тестовой базе остались клиенты с истёкшей подпиской")
    with pytest.raises(NoRecipientsError):
        await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)


async def test_11_1_wrong_content_is_not_confirmed(db_session: AsyncSession) -> None:
    """11.1: текст с ошибкой разметки или длиннее подписи с изображением не отправляется."""
    await make_client(db_session, telegram_id=7401)
    owner = await _owner(db_session)
    broken = await _draft(db_session, text="<b>x")
    with pytest.raises(FormatError):
        await confirm(db_session, broken, member_id=owner.id, now=NOW)
    long = await _draft(db_session, text="я" * (MAX_CAPTION_LENGTH + 1))
    await set_image(db_session, long, PNG)
    with pytest.raises(FormatError, match="1024"):
        await confirm(db_session, long, member_id=owner.id, now=NOW)


# --- Админка (А8) и бот ---


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        await put_setting(db_session, SHOP_STATE, ShopState.OPEN)
        yield shop


async def _sign_in(shop: Shop, role: TeamRole = TeamRole.OWNER) -> TeamMember:
    member = TeamMember(telegram_id=OWNER_TELEGRAM if role == TeamRole.OWNER else 2, role=role)
    existing = await shop.session.scalar(
        select(TeamMember).where(TeamMember.telegram_id == member.telegram_id)
    )
    if existing is None:
        await add(shop.session, member)
    else:
        member = existing
    await shop.sign_in(member)
    return member


def _draft_body(text: str = "<b>Скидки</b>", **content: Any) -> dict[str, Any]:
    return {"content": {"text": text, **content}, "segment": {"kind": "all"}}


async def test_11_3_test_message_to_owner(shop: Shop) -> None:
    """11.3, 11.11: «Отправить себе» — владелец получает то же сообщение, что клиенты."""
    owner = await _sign_in(shop)
    created = await shop.http.post("/api/admin/broadcasts", json=_draft_body(buy=True))
    assert created.status_code == 201
    broadcast_id = created.json()["id"]
    response = await shop.http.post(f"/api/admin/broadcasts/{broadcast_id}/test")
    assert response.status_code == 200, response.text
    [message] = [m for m in shop.telegram.sent() if m.chat_id == owner.telegram_id]
    assert message.text == "Скидки"
    assert message.entities is not None
    assert message.entities[0].type == "bold"
    assert [b.text for b in screen_buttons(message)] == ["Купить подписку", "Главное меню"]
    assert await _actions(shop.session, broadcast_id) == ["broadcast.test_sent"]

    # С изображением — фото с подписью; file_id запоминается
    upload = await shop.http.put(f"/api/admin/broadcasts/{broadcast_id}/image", content=PNG)
    assert upload.status_code == 200
    assert upload.json()["has_image"] is True
    await shop.http.post(f"/api/admin/broadcasts/{broadcast_id}/test")
    [photo] = [r for r in shop.telegram.requests if isinstance(r, SendPhoto)]
    assert photo.caption == "Скидки"
    image = await shop.session.get_one(BroadcastImage, broadcast_id)
    assert image.telegram_file_id is not None


async def test_11_3_test_message_problem_is_shown(shop: Shop) -> None:
    """11.3: ошибка разметки — понятный текст, сообщение не уходит."""
    await _sign_in(shop)
    created = await shop.http.post("/api/admin/broadcasts", json=_draft_body("<b>x"))
    response = await shop.http.post(f"/api/admin/broadcasts/{created.json()['id']}/test")
    assert response.status_code == 422
    assert "Не закрыт" in response.json()["detail"]
    assert shop.telegram.sent() == []


async def test_11_2_11_4_11_8_admin_flow(shop: Shop) -> None:
    """11.2, 11.4, 11.5, 11.7, 11.8: сегменты с числом получателей, подтверждение с
    числом получателей, прогресс, остановка, история с итогами; черновик меняется,
    отправленная рассылка — нет."""
    await _sign_in(shop)
    await stop_all_sending(shop.session)
    await make_client(shop.session, telegram_id=8001)
    segments = (await shop.http.get("/api/admin/broadcasts/segments")).json()["segments"]
    assert [segment["kind"] for segment in segments] == ["all", "active", "expired"]
    expected = segments[0]["recipients"]
    assert expected >= 1

    created = (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).json()
    broadcast_id = created["id"]
    assert created["state"] == "draft"
    edited = await shop.http.put(
        f"/api/admin/broadcasts/{broadcast_id}", json=_draft_body("Другой текст", renew=True)
    )
    assert edited.json()["content"]["renew"] is True

    started = await shop.http.post(f"/api/admin/broadcasts/{broadcast_id}/confirm")
    assert started.status_code == 200, started.text
    assert started.json()["recipients"] == expected
    progress = started.json()["broadcast"]["totals"]
    assert progress["pending"] == expected

    again = await shop.http.put(f"/api/admin/broadcasts/{broadcast_id}", json=_draft_body())
    assert again.status_code == 409

    stopped = await shop.http.post(f"/api/admin/broadcasts/{broadcast_id}/stop")
    assert stopped.json()["state"] == "stopped"
    assert stopped.json()["totals"]["skipped"] == expected

    history = (await shop.http.get("/api/admin/broadcasts")).json()["broadcasts"]
    assert history[0]["id"] == broadcast_id


async def test_11_13_second_broadcast_conflicts(shop: Shop) -> None:
    """11.13: пока идёт рассылка, вторую подтвердить нельзя."""
    await _sign_in(shop)
    await stop_all_sending(shop.session)
    await make_client(shop.session, telegram_id=8101)
    first = (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).json()["id"]
    second = (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).json()["id"]
    await shop.http.post(f"/api/admin/broadcasts/{first}/confirm")
    response = await shop.http.post(f"/api/admin/broadcasts/{second}/confirm")
    assert response.status_code == 409
    assert "Уже идёт" in response.json()["detail"]


async def test_broadcasts_are_for_owner_only(shop: Shop) -> None:
    """01-domain: рассылки — только владельцу; помощник их не видит."""
    await _sign_in(shop, TeamRole.ASSISTANT)
    assert (await shop.http.get("/api/admin/broadcasts")).status_code == 403
    assert (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).status_code == 403


async def test_11_1_image_must_be_photo(shop: Shop) -> None:
    """11.1: изображение — JPEG или PNG."""
    await _sign_in(shop)
    broadcast_id = (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).json()["id"]
    response = await shop.http.put(
        f"/api/admin/broadcasts/{broadcast_id}/image", content=b"GIF89a..."
    )
    assert response.status_code == 422


# --- 11.10, 11.12. Кнопки в боте ---

USER = telegram_user(9001)


async def _bot_client(shop: Shop) -> Client:
    client = await shop.session.scalar(
        select(Client).join(TelegramAccount).where(TelegramAccount.telegram_id == USER.id)
    )
    return client or await make_client(shop.session, telegram_id=USER.id)


async def _tariff(shop: Shop) -> Tariff:
    tariff = make_tariff(name="Месяц", price=Decimal("199.00"))
    await add(shop.session, tariff)
    return tariff


async def _subscribe(
    shop: Shop, tariff: Tariff | None, *, trial: bool = False, panel_user_id: int = 1
) -> None:
    client = await _bot_client(shop)
    subscription = make_subscription(client, tariff, panel_user_id=panel_user_id)
    subscription.expires_at = NOW + timedelta(days=5)
    subscription.panel_status = PanelUserStatus.ACTIVE
    subscription.is_trial = trial
    await add(shop.session, subscription)


async def _press(shop: Shop, data: str) -> SendMessage | SendRichMessage | EditMessageText:
    await shop.press(USER, data)
    return shop.telegram.last()


def _rich_text(screen: SendMessage | SendRichMessage | EditMessageText) -> str:
    assert isinstance(screen, SendRichMessage)
    return screen.rich_message.model_dump_json()


@pytest.mark.parametrize("button", [BUY_CALLBACK, RENEW_CALLBACK])
async def test_11_10_without_subscriptions_leads_to_tariffs(shop: Shop, button: str) -> None:
    """11.10: без подписок «Купить» и «Продлить» ведут к выбору тарифа — новым
    сообщением: рассылка не перезаписывается."""
    await _tariff(shop)
    await _bot_client(shop)
    screen = await _press(shop, button)
    assert isinstance(screen, SendRichMessage)
    assert "Месяц" in _rich_text(screen)


@pytest.mark.parametrize("button", [BUY_CALLBACK, RENEW_CALLBACK])
async def test_11_10_paid_subscription_leads_to_renewal(shop: Shop, button: str) -> None:
    """11.10: у клиента с платной подпиской «Купить» — как «Продлить»: подтверждение
    продления той же подписки."""
    tariff = await _tariff(shop)
    await _subscribe(shop, tariff)
    screen = await _press(shop, button)
    assert isinstance(screen, SendMessage)
    assert "Подписка будет действовать до" in (screen.text or "")


async def test_11_10_trial_only_leads_to_tariffs(shop: Shop) -> None:
    """11.10, 3.36: единственная подписка — триальная: «Продлить» ведёт к выбору тарифа."""
    tariff = await _tariff(shop)
    await _subscribe(shop, tariff, trial=True)
    screen = await _press(shop, RENEW_CALLBACK)
    assert isinstance(screen, SendRichMessage)


async def test_11_10_several_subscriptions_choose_one(shop: Shop) -> None:
    """11.10: подписок несколько — выбор подписки."""
    tariff = await _tariff(shop)
    await _subscribe(shop, tariff, panel_user_id=1)
    await _subscribe(shop, tariff, panel_user_id=2)
    screen = await _press(shop, RENEW_CALLBACK)
    assert isinstance(screen, SendMessage)
    assert len(screen_buttons(screen)) == 3  # две подписки и «Назад»


async def test_11_12_paused_shop(shop: Shop) -> None:
    """11.12: в закрытом магазине кнопки рассылки ведут к «Магазин временно закрыт»."""
    await _tariff(shop)
    await _bot_client(shop)
    await put_setting(shop.session, SHOP_STATE, ShopState.PAUSED)
    screen = await _press(shop, BUY_CALLBACK)
    assert isinstance(screen, SendMessage)
    assert "временно не принимает" in (screen.text or "")


async def test_11_12_not_opened_shop(shop: Shop) -> None:
    """11.12: в ещё не открытом магазине — «Магазин скоро откроется»; рассылка при этом
    разрешена."""
    await _bot_client(shop)
    await put_setting(shop.session, SHOP_STATE, ShopState.NOT_OPENED)
    screen = await _press(shop, RENEW_CALLBACK)
    assert isinstance(screen, SendMessage)
    assert "скоро откроется" in (screen.text or "")

    await _sign_in(shop)
    await stop_all_sending(shop.session)
    broadcast_id = (await shop.http.post("/api/admin/broadcasts", json=_draft_body())).json()["id"]
    started = await shop.http.post(f"/api/admin/broadcasts/{broadcast_id}/confirm")
    assert started.status_code == 200


async def test_11_11_journal_is_in_test_transaction(db_session: AsyncSession) -> None:
    """11.11: записи о рассылке — в общем журнале."""
    await make_client(db_session, telegram_id=9101)
    broadcast_id = await _draft(db_session)
    owner = await _owner(db_session)
    await stop_all_sending(db_session)
    await confirm(db_session, broadcast_id, member_id=owner.id, now=NOW)
    found = await db_session.scalar(
        select(func.count()).where(
            journaled_in_test(),
            JournalEntry.subject_type == "broadcast",
            JournalEntry.action == "broadcast.started",
        )
    )
    assert found
