"""Миграция: формат, усыновление, импорт, пробный прогон и применение (блок 9, решение 0071)."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client, TelegramAccount
from remnabay.domain.migration_runs import MigrationPanelUser, MigrationRun, MigrationRunState
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import TariffType, TrafficResetStrategy
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.domain.trials import Trial
from remnabay.journal import JournalEntry
from remnabay.migration import (
    FormatError,
    MappingRule,
    MigrationFile,
    MigrationReport,
    MigrationRunError,
    ProblemReason,
    RunError,
    apply_dry_run,
    parse_file,
    rerun,
    start_dry_run,
)
from remnabay.migration._runs import APPLY_BATCH, PAGE_SIZE, read_panel
from remnabay.queue import Ending
from remnabay.trial import trial_available
from tests.domain_support import add, make_client, make_subscription, make_tariff
from tests.guide_support import queued
from tests.migration_support import run_chain
from tests.panel_support import FakePanel, fake_runtime

SQUAD_A = "6f0b2c3e-1d2a-4b5c-8d9e-0f1a2b3c4d5e"
SQUAD_B = "7a1c3d4f-2e3b-4c6d-9e0f-1a2b3c4d5e6f"
FUTURE = (datetime.now(UTC) + timedelta(days=20)).isoformat()
PAST = (datetime.now(UTC) - timedelta(days=20)).isoformat()


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def runtime(panel: FakePanel) -> Generator[None]:
    with fake_runtime(panel=panel):
        yield


@pytest.fixture
async def owner(db_session: AsyncSession) -> TeamMember:
    member = TeamMember(telegram_id=1, role=TeamRole.OWNER)
    await add(db_session, member)
    return member


def _user(panel: FakePanel, user_id: int, telegram_id: int | None, **overrides: Any) -> None:
    squads = overrides.pop("squads", [SQUAD_A])
    default_name = f"tg_{telegram_id}_{user_id}" if telegram_id else f"web_{user_id}"
    panel.add_user(
        id=user_id,
        shortUuid=f"short{user_id}",
        username=overrides.pop("username", default_name),
        telegramId=telegram_id,
        subscriptionUrl=f"https://sub.example.com/short{user_id}",
        activeInternalSquads=[{"uuid": uuid, "name": f"Сквад {uuid[:4]}"} for uuid in squads],
        expireAt=overrides.pop("expireAt", FUTURE),
        **overrides,
    )


async def _dry_run(
    session: AsyncSession,
    owner: TeamMember,
    *,
    mapping: list[MappingRule] | None = None,
    file: MigrationFile | None = None,
) -> MigrationRun:
    run = await start_dry_run(session, member_id=owner.id, mapping=mapping or [], file=file)
    await run_chain(session)
    await session.refresh(run)
    return run


async def _apply(session: AsyncSession, owner: TeamMember, dry: MigrationRun) -> MigrationRun:
    run = await apply_dry_run(session, run_id=dry.id, member_id=owner.id, now=datetime.now(UTC))
    await run_chain(session)
    await session.refresh(run)
    return run


def _report(run: MigrationRun) -> MigrationReport:
    return MigrationReport.model_validate(run.report)


async def _client_of(session: AsyncSession, telegram_id: int) -> Client:
    return await session.scalar(
        select(Client).join(TelegramAccount).where(TelegramAccount.telegram_id == telegram_id)
    ) or pytest.fail(f"нет клиента {telegram_id}")


async def _subscriptions(session: AsyncSession, client: Client) -> list[Subscription]:
    rows = await session.scalars(
        select(Subscription).where(Subscription.client_id == client.id).order_by(Subscription.id)
    )
    return list(rows)


def _file(*clients: dict[str, Any]) -> MigrationFile:
    raw = {"format_version": 1, "source": {"name": "test-bot"}, "clients": list(clients)}
    return parse_file(json.dumps(raw).encode())


# --- Формат (9.8) ---


def test_9_8_file_version_1_with_and_without_paid_field() -> None:
    """9.8, 0070: файл версии 1 читается с полем «Платил в старом боте» и без него;
    неизвестные ключи пропускаются."""
    file = _file(
        {"telegram_id": 1, "paid_in_old_bot": False, "future_field": 1},
        {"telegram_id": 2, "panel_users": [{"username": "tg_2"}]},
    )
    assert [client.paid_in_old_bot for client in file.clients] == [False, None]
    assert file.clients[1].trial_used is False


def test_9_8_unknown_version_is_rejected_with_supported_list() -> None:
    """9.8: неизвестная версия отклоняется с перечнем поддерживаемых."""
    raw = json.dumps({"format_version": 2, "source": {"name": "x"}, "clients": []}).encode()
    with pytest.raises(FormatError, match="Поддерживаемые версии: 1"):
        parse_file(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"clients": []}',
        b'{"format_version": 1, "source": {"name": "x"}, "clients": [{"panel_users": [{}]}]}',
        b'{"format_version": 1, "source": {"name": "x"},'
        b' "clients": [{"telegram_id": 1, "panel_users": [{}]}]}',
    ],
)
def test_9_8_broken_file_is_rejected(raw: bytes) -> None:
    """9.8: файл не в формате — понятная ошибка, а не сбой."""
    with pytest.raises(FormatError):
        parse_file(raw)


# --- Усыновление (9.1–9.7, 9.15, 9.16) ---


@pytest.mark.usefixtures("runtime")
async def test_9_2_dry_run_report(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.1, 9.2, 0071: пробный прогон считает усыновляемых, клиентов, клиентов с
    несколькими подписками, пользователей без Telegram ID, уже бывших в магазине; в
    магазине при этом ничего не появляется."""
    _user(panel, 11, 1001, createdAt="2025-01-01T00:00:00Z")
    _user(panel, 12, 1001, createdAt="2025-02-01T00:00:00Z", status="EXPIRED", expireAt=PAST)
    _user(panel, 13, 1002, status="DISABLED")
    _user(panel, 14, None)
    _user(panel, 15, 1003)
    existing = await make_client(db_session, telegram_id=1003)
    _user(panel, 16, 1004)
    shop_made = await make_client(db_session, telegram_id=1004)
    await add(db_session, make_subscription(shop_made, None, panel_user_id=16))

    run = await _dry_run(db_session, owner)

    report = _report(run)
    assert run.state == MigrationRunState.DONE
    assert (report.panel_users, report.adopt, report.clients) == (6, 4, 3)
    assert (report.clients_new, report.clients_existing, report.clients_several) == (2, 1, 1)
    assert (report.without_telegram_id, report.already_in_shop) == (1, 1)
    assert await db_session.scalar(select(func.count()).select_from(Client)) == 2
    assert await _subscriptions(db_session, existing) == []
    # Снимок панели удалён вместе с концом прогона
    assert await db_session.scalar(select(func.count()).select_from(MigrationPanelUser)) == 0


@pytest.mark.usefixtures("runtime")
async def test_9_4_9_5_9_16_apply_adoption(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.4, 9.5, 9.16: пользователи с одним Telegram ID — один клиент с несколькими
    подписками по дате создания в панели; срок — отрезок нулевой стоимости; клиент
    помечен усыновлённым; истёкший пользователь тоже усыновляется, без отрезка."""
    _user(panel, 22, 1001, createdAt="2025-02-01T00:00:00Z", status="EXPIRED", expireAt=PAST)
    _user(panel, 21, 1001, createdAt="2025-01-01T00:00:00Z")

    applied = await _apply(db_session, owner, await _dry_run(db_session, owner))

    assert applied.state == MigrationRunState.DONE
    client = await _client_of(db_session, 1001)
    assert client.adopted_at is not None
    assert client.first_seen_at == datetime(2025, 1, 1, tzinfo=UTC)
    first, second = await _subscriptions(db_session, client)
    assert (first.panel_user_id, first.name) == (21, "Основная")
    assert (second.panel_user_id, second.name) == (22, "Подписка 2")
    assert first.tariff_id is None
    assert first.adopted_at is not None
    segments = list(await db_session.scalars(select(TermSegment)))
    assert [(s.subscription_id, s.kind, s.cost) for s in segments] == [
        (first.id, SegmentKind.MIGRATED, 0)
    ]


@pytest.mark.usefixtures("runtime")
async def test_9_6_adoption_changes_nothing_in_panel(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.6: усыновление в панели только читает."""
    _user(panel, 31, 1001)
    _user(panel, 32, None)

    await _apply(db_session, owner, await _dry_run(db_session, owner))

    assert panel.writes == []
    assert all(request.startswith("GET ") for request in panel.requests)


@pytest.mark.usefixtures("runtime")
async def test_9_4_existing_client_gets_subscription_and_mark(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.4, 9.16, 0071: клиент уже есть в магазине — пользователь панели становится его
    подпиской с продолжением нумерации, клиент получает пометку «усыновлён»."""
    client = await make_client(db_session, telegram_id=1001)
    await add(db_session, make_subscription(client, None, panel_user_id=40))
    _user(panel, 41, 1001)

    await _apply(db_session, owner, await _dry_run(db_session, owner))

    assert client.adopted_at is not None
    names = [subscription.name for subscription in await _subscriptions(db_session, client)]
    assert names == ["Основная", "Подписка 2"]


@pytest.mark.usefixtures("runtime")
async def test_9_7_repeated_adoption_creates_no_duplicates(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.7: повторный запуск пропускает уже усыновлённых."""
    _user(panel, 51, 1001)
    await _apply(db_session, owner, await _dry_run(db_session, owner))
    _user(panel, 52, 1002)

    again = await _dry_run(db_session, owner)
    report = _report(again)
    await _apply(db_session, owner, again)

    assert (report.adopt, report.already_adopted) == (1, 1)
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Client)) == 2


@pytest.mark.usefixtures("runtime")
async def test_9_15_mapping_groups_and_differences(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.15, 9.2, 0071: прогон делит пользователей на группы «сквады + лимит устройств»;
    сопоставленные получают тариф, остальные — «без тарифа»; отчёт считает, у скольких
    параметры отличаются от тарифа."""
    tariff = make_tariff(
        type=TariffType.TERM_QUOTA,
        squad_uuids=[UUID(SQUAD_A)],
        device_limit=3,
        traffic_limit_bytes=100,
        traffic_reset_strategy=TrafficResetStrategy.MONTH,
    )
    await add(db_session, tariff)
    _user(panel, 61, 1001, trafficLimitBytes=100, trafficLimitStrategy="MONTH")
    _user(panel, 62, 1002, trafficLimitBytes=0)
    _user(panel, 63, 1003, squads=[SQUAD_A, SQUAD_B], hwidDeviceLimit=5)

    first = await _dry_run(db_session, owner)
    groups = _report(first).groups
    assert [(len(g.squads), g.device_limit, g.users, g.tariff_id) for g in groups] == [
        (1, 3, 2, None),
        (2, 5, 1, None),
    ]
    rule = MappingRule(squads=[UUID(SQUAD_A)], device_limit=3, tariff_id=tariff.id)
    second = await rerun(db_session, run_id=first.id, member_id=owner.id, mapping=[rule])
    await run_chain(db_session)
    await db_session.refresh(second)
    report = _report(second)
    assert (report.no_tariff, report.differs) == (1, 1)
    assert report.groups[0].tariff_id == tariff.id

    await _apply(db_session, owner, second)
    tariffs = {
        subscription.panel_user_id: subscription.tariff_id
        for subscription in await db_session.scalars(select(Subscription))
    }
    assert tariffs == {61: tariff.id, 62: tariff.id, 63: None}


@pytest.mark.usefixtures("runtime")
async def test_9_3_apply_only_latest_fresh_dry_run(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.3, 0071: применить можно только последний пробный прогон своего вида, не старше
    суток и один раз; одновременно идёт один прогон."""
    _user(panel, 71, 1001)
    old = await _dry_run(db_session, owner)
    latest = await _dry_run(db_session, owner)
    now = datetime.now(UTC)

    with pytest.raises(MigrationRunError) as not_latest:
        await apply_dry_run(db_session, run_id=old.id, member_id=owner.id, now=now)
    assert not_latest.value.reason == RunError.NOT_LATEST
    with pytest.raises(MigrationRunError) as expired:
        await apply_dry_run(
            db_session, run_id=latest.id, member_id=owner.id, now=now + timedelta(days=2)
        )
    assert expired.value.reason == RunError.EXPIRED

    await apply_dry_run(db_session, run_id=latest.id, member_id=owner.id, now=now)
    with pytest.raises(MigrationRunError) as busy:
        await start_dry_run(db_session, member_id=owner.id, mapping=[])
    assert busy.value.reason == RunError.BUSY
    await run_chain(db_session)
    with pytest.raises(MigrationRunError) as applied:
        await apply_dry_run(db_session, run_id=latest.id, member_id=owner.id, now=now)
    assert applied.value.reason == RunError.ALREADY_APPLIED


@pytest.mark.usefixtures("runtime")
async def test_9_3_apply_reads_panel_again(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.3: применение заново читает панель: появившийся после пробного прогона
    пользователь тоже усыновляется, итоговый отчёт — фактический."""
    _user(panel, 81, 1001)
    dry = await _dry_run(db_session, owner)
    _user(panel, 82, 1002)

    applied = await _apply(db_session, owner, dry)

    assert (_report(dry).adopt, _report(applied).adopt) == (1, 2)
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 2


@pytest.mark.usefixtures("runtime")
async def test_4_28_large_panel_is_read_and_applied_in_steps(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """4.28: снимок панели читается страницами, применение идёт порциями клиентов."""
    count = PAGE_SIZE + APPLY_BATCH + 1
    for index in range(count):
        _user(panel, 1000 + index, 50_000 + index)

    run = await start_dry_run(db_session, member_id=owner.id, mapping=[])
    await run_chain(db_session)
    await db_session.refresh(run)
    applied = await _apply(db_session, owner, run)

    pages = [r for r in panel.requests if r == "GET /api/users"]
    assert len(pages) == 4  # по две страницы на пробный и на настоящий прогон
    steps = await db_session.scalar(
        select(func.count()).where(JournalEntry.action == "client.adopted")
    )
    assert (_report(applied).adopt, steps) == (count, count)


@pytest.mark.usefixtures("runtime")
async def test_9_14_runs_and_adoptions_are_journaled(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.14, 4.25: пробный прогон, применение, усыновление клиента и подписки — в журнале
    от имени участника команды."""
    _user(panel, 91, 1001)

    await _apply(db_session, owner, await _dry_run(db_session, owner))

    rows = await db_session.execute(select(JournalEntry.action, JournalEntry.actor_ref))
    actions = {action: actor for action, actor in rows}
    for action in ("migration.dry_run", "migration.applied", "client.adopted"):
        assert actions[action] == str(owner.id)
    assert actions["subscription.adopted"] == str(owner.id)


@pytest.mark.usefixtures("runtime")
async def test_9_3_failed_run_frees_the_way(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.3: если прогон прервался, он помечается неудавшимся и не мешает следующему."""
    _user(panel, 95, 1001)
    run = await start_dry_run(db_session, member_id=owner.id, mapping=[])
    [task] = await queued(db_session, read_panel)
    await read_panel.ended(Ending.FAILED, db_session, task.id, task.args)
    await db_session.refresh(run)

    assert run.state == MigrationRunState.FAILED
    assert run.finished_at is not None
    await start_dry_run(db_session, member_id=owner.id, mapping=[])


@pytest.mark.usefixtures("runtime")
async def test_9_15_unknown_tariff_in_mapping_is_rejected(
    db_session: AsyncSession, owner: TeamMember
) -> None:
    """9.15: сопоставление ссылается только на существующие тарифы."""
    rule = MappingRule(squads=[UUID(SQUAD_A)], device_limit=3, tariff_id=999_999)
    with pytest.raises(MigrationRunError) as error:
        await start_dry_run(db_session, member_id=owner.id, mapping=[rule])
    assert error.value.reason == RunError.UNKNOWN_TARIFF


# --- Импорт (9.10–9.13, 9.17) ---


@pytest.mark.usefixtures("runtime")
async def test_9_10_9_11_import_report_and_apply(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.10, 9.11, 0071: импорт усыновляет пользователей из файла и вне его; связь из
    файла с пользователем без Telegram ID в панели — не конфликт; отчёт считает связи,
    коды, пригласивших, клиентов без пользователя в панели."""
    _user(panel, 101, 2001)
    _user(panel, 102, None, username="tg_2002")
    _user(panel, 103, 2003)  # нет в файле
    file = _file(
        {
            "telegram_id": 2001,
            "panel_users": [{"id": 101}],
            "referral_codes": ["ref_uAAA", "ref_AAA"],
            "paid_in_old_bot": True,
            "first_seen_at": "2024-05-01T10:00:00+00:00",
            "language": "en",
        },
        {
            "telegram_id": 2002,
            "panel_users": [{"username": "tg_2002"}],
            "invited_by": 2001,
            "paid_in_old_bot": False,
        },
        {"telegram_id": 2004, "panel_users": [{"short_uuid": "gone"}], "trial_used": True},
    )

    dry = await _dry_run(db_session, owner, file=file)
    report = _report(dry)
    assert report.file is not None
    assert (report.adopt, report.clients) == (3, 3)
    assert (report.file.links_found, report.file.links_not_found) == (2, 1)
    assert report.file.links_without_telegram_id == 1
    assert (report.file.referral_codes, report.file.referral_links) == (2, 1)
    assert report.file.without_panel_user == 1
    assert (report.file.paid_yes, report.file.paid_no, report.file.paid_unknown) == (1, 1, 1)
    assert [(p.telegram_id, p.reason) for p in report.problems] == [
        (2004, ProblemReason.PANEL_USER_NOT_FOUND)
    ]

    await _apply(db_session, owner, dry)

    inviter = await _client_of(db_session, 2001)
    assert (inviter.old_bot_paid, inviter.imported_at is not None) == (True, True)
    assert inviter.first_seen_at == datetime(2024, 5, 1, 10, tzinfo=UTC)
    assert inviter.language_code == "en"
    invited = await _client_of(db_session, 2002)
    assert [s.panel_user_id for s in await _subscriptions(db_session, invited)] == [102]
    assert invited.old_bot_paid is False
    link = await db_session.get_one(ReferralLink, invited.id)
    assert (link.inviter_id, link.migrated) == (inviter.id, True)
    codes = await db_session.scalars(select(ReferralCode).where(ReferralCode.migrated))
    assert {(c.code, c.client_id) for c in codes} == {
        ("ref_uAAA", inviter.id),
        ("ref_AAA", inviter.id),
    }
    plain = await _client_of(db_session, 2003)
    assert (plain.adopted_at is not None, plain.imported_at) == (True, None)
    assert panel.writes == []


@pytest.mark.usefixtures("runtime")
async def test_9_17_client_without_panel_user(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.17, 1.17: клиент из файла без пользователя в панели — обычный клиент, не
    усыновлённый; если он использовал триал, триал ему недоступен."""
    file = _file(
        {"telegram_id": 3001, "trial_used": True, "username": "ivan", "name": "Иван"},
        {"telegram_id": 3002},
    )

    await _apply(db_session, owner, await _dry_run(db_session, owner, file=file))

    used = await _client_of(db_session, 3001)
    assert (used.adopted_at, used.imported_at is not None) == (None, True)
    trial = await db_session.get_one(Trial, used.id)
    assert (trial.imported, trial.subscription_id) == (True, None)
    account = await db_session.get_one(TelegramAccount, 3001)
    assert (account.username, account.first_name) == ("ivan", "Иван")
    assert not await trial_available(db_session, used)
    assert await db_session.get(Trial, (await _client_of(db_session, 3002)).id) is None


@pytest.mark.usefixtures("runtime")
async def test_9_12_conflicts_are_shown_and_not_applied(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.12: пользователь панели уже привязан к другому клиенту или его Telegram ID в
    панели другой — конфликт: в отчёте и не применяется; код, который уже ведёт на
    другого клиента, — тоже."""
    other = await make_client(db_session, telegram_id=4999)
    await add(db_session, make_subscription(other, None, panel_user_id=401))
    await add(db_session, ReferralCode(code="ref_uTAKEN", client_id=other.id))
    _user(panel, 401, 4999)
    _user(panel, 402, 4002)
    file = _file(
        {"telegram_id": 4001, "panel_users": [{"id": 401}], "referral_codes": ["ref_uTAKEN"]},
        {"telegram_id": 4003, "panel_users": [{"id": 402}]},
    )

    dry = await _dry_run(db_session, owner, file=file)
    report = _report(dry)
    await _apply(db_session, owner, dry)

    reasons = sorted((p.telegram_id or 0, p.reason) for p in report.problems)
    assert reasons == [
        (4001, ProblemReason.CODE_TAKEN),
        (4001, ProblemReason.TELEGRAM_ID_MISMATCH),
        (4003, ProblemReason.TELEGRAM_ID_MISMATCH),
    ]
    assert report.conflicts == 3
    owners = {s.panel_user_id: s.client_id for s in await db_session.scalars(select(Subscription))}
    assert owners == {401: other.id}
    code = await db_session.get_one(ReferralCode, "ref_uTAKEN")
    assert code.client_id == other.id


@pytest.mark.usefixtures("runtime")
async def test_9_11_unrecognized_records(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.11, 0071: повтор Telegram ID в файле, неположительный ID, неизвестный
    пригласивший и приглашение самого себя — в «не распознано»."""
    file = _file(
        {"telegram_id": 5001},
        {"telegram_id": 5001},
        {"telegram_id": -5},
        {"telegram_id": 5002, "invited_by": 9999},
        {"telegram_id": 5003, "invited_by": 5003},
    )

    dry = await _dry_run(db_session, owner, file=file)
    report = _report(dry)
    await _apply(db_session, owner, dry)

    assert sorted((p.telegram_id, p.reason) for p in report.problems) == [
        (-5, ProblemReason.BAD_TELEGRAM_ID),
        (5001, ProblemReason.DUPLICATE_TELEGRAM_ID),
        (5001, ProblemReason.DUPLICATE_TELEGRAM_ID),
        (5002, ProblemReason.INVITER_NOT_FOUND),
        (5003, ProblemReason.SELF_INVITE),
    ]
    assert report.unrecognized == 5
    imported = await db_session.scalars(select(TelegramAccount.telegram_id))
    assert sorted(imported) == [5002, 5003]


@pytest.mark.usefixtures("runtime")
async def test_9_7_import_fills_once_and_skips_imported(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.7, 0070, 0071: клиента, усыновлённого без файла, импорт дополняет один раз;
    повторный импорт уже перенесённых пропускает — их пометка не меняется."""
    _user(panel, 601, 6001)
    await _apply(db_session, owner, await _dry_run(db_session, owner))
    client = await _client_of(db_session, 6001)
    assert client.old_bot_paid is None

    first = _file({"telegram_id": 6001, "paid_in_old_bot": False, "trial_used": True})
    dry = await _dry_run(db_session, owner, file=first)
    file_report = _report(dry).file
    assert file_report is not None
    assert file_report.filled == 1
    await _apply(db_session, owner, dry)
    assert (client.old_bot_paid, client.imported_at is not None) == (False, True)
    assert await db_session.get(Trial, client.id) is not None

    second = _file({"telegram_id": 6001, "paid_in_old_bot": True})
    dry = await _dry_run(db_session, owner, file=second)
    file_report = _report(dry).file
    assert file_report is not None
    assert file_report.skipped_imported == 1
    await _apply(db_session, owner, dry)
    assert client.old_bot_paid is False
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 1


@pytest.mark.usefixtures("runtime")
async def test_9_11_existing_referral_link_is_kept(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.11, 3.15, 0071: реферальную связь, которая уже есть, импорт не меняет."""
    first_inviter = await make_client(db_session, telegram_id=7000)
    invited = await make_client(db_session, telegram_id=7001)
    await add(db_session, ReferralLink(invited_id=invited.id, inviter_id=first_inviter.id))
    file = _file({"telegram_id": 7002}, {"telegram_id": 7001, "invited_by": 7002})

    await _apply(db_session, owner, await _dry_run(db_session, owner, file=file))

    link = await db_session.get_one(ReferralLink, invited.id)
    assert link.inviter_id == first_inviter.id


@pytest.mark.usefixtures("runtime")
async def test_9_15_tariff_hints_are_shown_by_group(
    db_session: AsyncSession, panel: FakePanel, owner: TeamMember
) -> None:
    """9.15, migration-format.md: подсказка тарифа из файла показывается у группы."""
    _user(panel, 801, 8001)
    file = _file({"telegram_id": 8001, "panel_users": [{"id": 801, "tariff_hint": "Месяц"}]})

    report = _report(await _dry_run(db_session, owner, file=file))

    assert report.groups[0].hints == {"Месяц": 1}
