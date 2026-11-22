"""Модель данных, часть 2: промокоды, триал, реестр устройств, рефералы, рассылки,
напоминания, прогоны миграции, события панели (0015).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.broadcasts import Broadcast, BroadcastRecipient
from remnabay.domain.migration_runs import MigrationKind, MigrationRun
from remnabay.domain.panel_events import PanelEvent
from remnabay.domain.payments import PaymentState
from remnabay.domain.promo import (
    ActivationState,
    PromoActivation,
    PromoCode,
    PromoKind,
    promo_code_tariffs,
)
from remnabay.domain.referrals import ReferralCode, ReferralLink
from remnabay.domain.reminders import Reminder
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.domain.trials import DeviceRecord, Trial
from tests.domain_support import (
    NOW,
    add,
    assert_rejected,
    make_client,
    make_payment,
    make_subscription,
    make_tariff,
)

# --- Промокоды (блок 8) ---


@pytest.mark.parametrize(
    "values",
    [
        {"kind": PromoKind.PERCENT_DISCOUNT, "discount_percent": 1},
        {"kind": PromoKind.PERCENT_DISCOUNT, "discount_percent": 100},
        {"kind": PromoKind.DAYS, "days": 7},
        {"kind": PromoKind.FIXED_DISCOUNT, "discount_amount": Decimal("50.00")},
        {"kind": PromoKind.BONUS, "bonus_amount": Decimal("100.00")},
    ],
    ids=["percent_1", "percent_100", "days", "fixed_discount", "bonus"],
)
async def test_8_1_promo_code_stores_kind_and_value(
    db_session: AsyncSession, values: dict[str, object]
) -> None:
    """8.1: промокод хранит вид и значение; скидка — от 1 до 100%."""
    await add(db_session, PromoCode(code="SALE", **values))


@pytest.mark.parametrize(
    "values",
    [
        {"kind": PromoKind.PERCENT_DISCOUNT, "discount_percent": 0},
        {"kind": PromoKind.PERCENT_DISCOUNT, "discount_percent": 101},
        {"kind": PromoKind.PERCENT_DISCOUNT, "days": 7},
        {"kind": PromoKind.DAYS, "days": 7, "discount_percent": 10},
        {"kind": PromoKind.DAYS, "days": 0},
    ],
    ids=["percent_0", "percent_101", "percent_without_value", "two_values", "zero_days"],
)
async def test_8_1_promo_code_value_must_match_kind(
    db_session: AsyncSession, values: dict[str, object]
) -> None:
    """8.1: у вида — ровно своё значение в допустимых пределах."""
    await assert_rejected(db_session, PromoCode(code="SALE", **values))


async def test_8_2_promo_code_limits_and_tariffs(db_session: AsyncSession) -> None:
    """8.2: ограничения промокода — срок, число активаций, «без оплат», выбранные тарифы."""
    tariff = make_tariff()
    promo = PromoCode(
        code="FIRST",
        kind=PromoKind.PERCENT_DISCOUNT,
        discount_percent=20,
        valid_until=NOW + timedelta(days=30),
        max_activations=100,
        only_without_payments=True,
    )
    await add(db_session, tariff, promo)
    await db_session.execute(
        promo_code_tariffs.insert().values(promo_code_id=promo.id, tariff_id=tariff.id)
    )

    tariff_ids = await db_session.scalars(
        select(promo_code_tariffs.c.tariff_id).where(promo_code_tariffs.c.promo_code_id == promo.id)
    )
    assert list(tariff_ids) == [tariff.id]


async def test_8_5_one_promo_activation_per_payment(db_session: AsyncSession) -> None:
    """8.5: к одному платежу применяется не больше одного промокода."""
    tariff = make_tariff()
    first = PromoCode(code="A", kind=PromoKind.PERCENT_DISCOUNT, discount_percent=10)
    second = PromoCode(code="B", kind=PromoKind.PERCENT_DISCOUNT, discount_percent=20)
    await add(db_session, tariff, first, second)
    client = await make_client(db_session)
    payment = make_payment(client, tariff, promo_code_id=first.id, state=PaymentState.APPLIED)
    await add(db_session, payment)
    activation = {
        "client_id": client.id,
        "payment_id": payment.id,
        "state": ActivationState.APPLIED,
    }

    await add(db_session, PromoActivation(promo_code_id=first.id, **activation))
    await assert_rejected(db_session, PromoActivation(promo_code_id=second.id, **activation))


async def test_3_11_same_client_may_get_discount_on_both_paid_invoices(
    db_session: AsyncSession,
) -> None:
    """3.11: если клиент оплатил оба счёта одной покупки с одним промокодом, скидку
    получают оба — база не запрещает вторую активацию, превышение пишется в журнал."""
    tariff = make_tariff()
    promo = PromoCode(code="A", kind=PromoKind.PERCENT_DISCOUNT, discount_percent=10)
    await add(db_session, tariff, promo)
    client = await make_client(db_session)
    payments = [make_payment(client, tariff, promo_code_id=promo.id) for _ in range(2)]
    await add(db_session, *payments)

    await add(
        db_session,
        *(
            PromoActivation(
                promo_code_id=promo.id,
                client_id=client.id,
                payment_id=paid.id,
                state=ActivationState.APPLIED,
            )
            for paid in payments
        ),
    )


async def test_8_10_days_activation_targets_subscription(db_session: AsyncSession) -> None:
    """8.10, 8.15: промокод на дни активируется для подписки; пока дни ждут панель —
    активация в ожидании. Активация без платежа и подписки не допускается."""
    promo = PromoCode(code="DAYS", kind=PromoKind.DAYS, days=7)
    await add(db_session, promo)
    client = await make_client(db_session)
    subscription = make_subscription(client, None)
    await add(db_session, subscription)
    base = {"promo_code_id": promo.id, "client_id": client.id, "state": ActivationState.PENDING}

    await add(db_session, PromoActivation(**base, subscription_id=subscription.id))
    await assert_rejected(db_session, PromoActivation(**base))


# --- Триал и реестр устройств (блок 5) ---


async def test_5_5_one_trial_per_client(db_session: AsyncSession) -> None:
    """5.5: триал принадлежит клиенту — второй триал не создаётся."""
    client = await make_client(db_session)
    subscription = make_subscription(client, None)
    await add(db_session, subscription)
    await add(db_session, Trial(client_id=client.id, subscription_id=subscription.id))

    await assert_rejected(db_session, Trial(client_id=client.id))


async def test_9_17_imported_trial_without_subscription(db_session: AsyncSession) -> None:
    """9.17: факт использования триала, перенесённый из старого бота, — без подписки."""
    client = await make_client(db_session)

    await add(db_session, Trial(client_id=client.id, imported=True))


async def test_5_7_device_record_per_subscription(db_session: AsyncSession) -> None:
    """5.7, 5.9: реестр хранит HWID, клиента, подписку и дату подключения; одно
    устройство подписки — одна запись, а не новая при каждом событии."""
    client = await make_client(db_session)
    subscription = make_subscription(client, None)
    await add(db_session, subscription)
    record = {"hwid": "hw-1", "client_id": client.id, "subscription_id": subscription.id}

    await add(db_session, DeviceRecord(**record, connected_at=NOW))
    await assert_rejected(db_session, DeviceRecord(**record, connected_at=NOW + timedelta(hours=1)))


# --- Рефералы ---


async def test_3_15_one_inviter_and_not_self(db_session: AsyncSession) -> None:
    """3.15: у клиента не больше одного пригласившего; пригласить самого себя нельзя."""
    inviter = await make_client(db_session, telegram_id=1)
    other = await make_client(db_session, telegram_id=2)
    invited = await make_client(db_session, telegram_id=3)
    await add(db_session, ReferralLink(invited_id=invited.id, inviter_id=inviter.id))

    await assert_rejected(db_session, ReferralLink(invited_id=invited.id, inviter_id=other.id))
    await assert_rejected(db_session, ReferralLink(invited_id=other.id, inviter_id=other.id))


async def test_9_13_referral_code_leads_to_one_client(db_session: AsyncSession) -> None:
    """9.13: свой код и перенесённые коды ведут на клиента; один код — один клиент."""
    first = await make_client(db_session, telegram_id=1)
    second = await make_client(db_session, telegram_id=2)
    await add(
        db_session,
        ReferralCode(code="own-1", client_id=first.id),
        ReferralCode(code="old-bot-777", client_id=first.id, migrated=True),
    )

    await assert_rejected(db_session, ReferralCode(code="old-bot-777", client_id=second.id))


# --- Рассылка (блок 11) ---


async def test_11_6_recipient_gets_broadcast_at_most_once(db_session: AsyncSession) -> None:
    """11.4, 11.6: получатели фиксируются списком; каждый — не больше одного раза."""
    owner = TeamMember(telegram_id=100500, role=TeamRole.OWNER)
    await add(db_session, owner)
    client = await make_client(db_session)
    broadcast = Broadcast(
        content={"text": "Работы на сервере"},
        segment={"kind": "all"},
        created_by_id=owner.id,
    )
    await add(db_session, broadcast)

    await add(db_session, BroadcastRecipient(broadcast_id=broadcast.id, client_id=client.id))
    await assert_rejected(
        db_session, BroadcastRecipient(broadcast_id=broadcast.id, client_id=client.id)
    )


# --- Напоминания (блок 7) ---


async def test_7_8_reminder_is_planned_once_per_end_date_and_offset(
    db_session: AsyncSession,
) -> None:
    """7.8, 7.9: одно напоминание на подписку, дату окончания и смещение; с новой датой
    окончания напоминания планируются заново."""
    client = await make_client(db_session)
    subscription = make_subscription(client, None)
    await add(db_session, subscription)
    expires_at = NOW + timedelta(days=30)

    def reminder(end: object) -> Reminder:
        return Reminder(
            subscription_id=subscription.id,
            offset_hours=24,
            expires_at=end,
            due_at=NOW + timedelta(days=29),
        )

    await add(db_session, reminder(expires_at))
    await assert_rejected(db_session, reminder(expires_at))
    await add(db_session, reminder(expires_at + timedelta(days=30)))


# --- Прогон миграции и события панели ---


async def test_migration_run_stores_kind_mapping_report_and_author(
    db_session: AsyncSession,
) -> None:
    """Прогон миграции: вид, пробный или настоящий, сопоставление тарифов, отчёт, кто запустил."""
    owner = TeamMember(telegram_id=100500, role=TeamRole.OWNER)
    await add(db_session, owner)
    run = MigrationRun(
        kind=MigrationKind.ADOPTION,
        dry_run=True,
        tariff_mapping=[{"squads": ["a"], "device_limit": 3, "tariff_id": 1}],
        report={"adopted": 10, "without_telegram_id": 2},
        started_by_id=owner.id,
    )
    await add(db_session, run)

    stored = await db_session.scalar(select(MigrationRun).where(MigrationRun.id == run.id))
    assert stored is not None
    assert stored.report == {"adopted": 10, "without_telegram_id": 2}
    assert stored.source is None


async def test_4_2_repeated_panel_event_is_recognised(db_session: AsyncSession) -> None:
    """4.2: повтор того же события панели узнаётся по телу запроса."""
    event = {"body_hash": b"\x01" * 32, "event": "user.modified", "payload": {"id": 1}}

    await add(db_session, PanelEvent(**event))
    await assert_rejected(db_session, PanelEvent(**event))
