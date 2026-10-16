"""Промокоды: создание, ограничения, проверка кода, скидка, защита от перебора и API
админки (блок 8: 8.1–8.4, 8.12, 8.13, 8.16; решения 0065, 0066)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.clients import Client
from remnabay.domain.payments import PaymentState
from remnabay.domain.promo import ActivationState, PromoActivation, PromoCode, PromoKind
from remnabay.domain.subscriptions import PanelUserStatus
from remnabay.domain.tariffs import Tariff
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.payments import discounted_total
from remnabay.promo import (
    GENERATED_ALPHABET,
    CodeTakenError,
    NewPromo,
    PromoCodeError,
    PromoLimits,
    PromoRejectedError,
    PromoState,
    Rejection,
    blocked_minutes,
    create_promo,
    days_targets,
    enter_days,
    enter_discount,
    generate_code,
    list_promos,
    set_disabled,
    update_limits,
    without_payments,
)
from tests.conftest import journaled_in_test
from tests.domain_support import (
    add,
    make_client,
    make_payment,
    make_subscription,
    make_tariff,
    make_team_member,
)
from tests.web_support import Shop, running_shop

NOW = datetime.now(UTC)
API = "/api/admin"


@pytest.fixture
async def owner(db_session: AsyncSession) -> TeamMember:
    member = make_team_member()
    await add(db_session, member)
    return member


@pytest.fixture
async def tariff(db_session: AsyncSession) -> Tariff:
    tariff = make_tariff()
    await add(db_session, tariff)
    return tariff


async def _promo(
    session: AsyncSession,
    owner: TeamMember,
    code: str = "SALE15",
    kind: PromoKind = PromoKind.PERCENT_DISCOUNT,
    value: int = 15,
    *,
    valid_until: datetime | None = None,
    max_activations: int | None = None,
    only_without_payments: bool = False,
    tariff_ids: tuple[int, ...] = (),
) -> PromoCode:
    limits = PromoLimits(
        valid_until=valid_until,
        max_activations=max_activations,
        only_without_payments=only_without_payments,
        tariff_ids=tariff_ids,
    )
    new = NewPromo(code=code, kind=kind, value=value, limits=limits)
    return await create_promo(session, new, member_id=owner.id)


async def _activate(session: AsyncSession, promo: PromoCode, client: Client) -> None:
    payment = make_payment(client, None, state=PaymentState.APPLIED, applied_at=NOW)
    await add(session, payment)
    await add(
        session,
        PromoActivation(
            promo_code_id=promo.id,
            client_id=client.id,
            payment_id=payment.id,
            state=ActivationState.APPLIED,
            applied_at=NOW,
        ),
    )


async def _rejected_discount(
    session: AsyncSession, client: Client, code: str, tariff: Tariff
) -> Rejection:
    with pytest.raises(PromoRejectedError) as raised:
        await enter_discount(session, client, code, tariff, NOW)
    return raised.value.reason


# --- Создание (8.1, 8.2, 8.16, 8.3) ---


async def test_8_1_code_is_case_insensitive(db_session: AsyncSession, owner: TeamMember) -> None:
    """8.1, 0065: код хранится в верхнем регистре без пробелов по краям; `sale10` и
    `SALE10` — один код."""
    promo = await _promo(db_session, owner, code="  sale10 ")
    assert promo.code == "SALE10"
    with pytest.raises(CodeTakenError):
        await _promo(db_session, owner, code="Sale10")


@pytest.mark.parametrize("code", ["скидка", "SALE 10", "SALE_10", "", "A" * 65])
async def test_8_1_code_letters_digits_dash(
    db_session: AsyncSession, owner: TeamMember, code: str
) -> None:
    """8.1: в коде — латинские буквы, цифры и дефис."""
    with pytest.raises(PromoCodeError):
        await _promo(db_session, owner, code=code)


async def test_8_1_generated_code_has_no_similar_symbols(db_session: AsyncSession) -> None:
    """8.1, 0065: сгенерированный код — 8 знаков без похожих символов (0 и O, 1 и I)."""
    codes = [await generate_code(db_session) for _ in range(50)]
    for code in codes:
        assert len(code) == 8
        assert set(code) <= set(GENERATED_ALPHABET)
    assert not set("".join(codes)) & set("0O1I")


@pytest.mark.parametrize(
    ("kind", "value"),
    [(PromoKind.PERCENT_DISCOUNT, 0), (PromoKind.PERCENT_DISCOUNT, 101), (PromoKind.DAYS, 0)],
)
async def test_8_1_value_range(
    db_session: AsyncSession, owner: TeamMember, kind: PromoKind, value: int
) -> None:
    """8.1: скидка — от 1 до 100%; дней — хотя бы один."""
    with pytest.raises(PromoCodeError):
        await _promo(db_session, owner, kind=kind, value=value)


async def test_8_1_v1_kinds_are_not_created(db_session: AsyncSession, owner: TeamMember) -> None:
    """8.1, «Вне блока»: фиксированная скидка и бонусы — v1."""
    with pytest.raises(PromoCodeError):
        await _promo(db_session, owner, kind=PromoKind.FIXED_DISCOUNT, value=100)


async def test_8_2_tariffs_only_for_discount(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.2: «только для выбранных тарифов» — ограничение скидки."""
    with pytest.raises(PromoCodeError):
        await _promo(db_session, owner, kind=PromoKind.DAYS, value=7, tariff_ids=(tariff.id,))
    promo = await _promo(db_session, owner, tariff_ids=(tariff.id,))
    [view] = await list_promos(db_session, NOW)
    assert view.promo is promo
    assert view.tariff_ids == [tariff.id]


async def test_8_16_limits_change_any_time(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.16: ограничения меняются в любой момент; код, вид и значение — нет. Действие
    команды — в журнал (4.25)."""
    promo = await _promo(db_session, owner, max_activations=10)
    until = NOW + timedelta(days=7)
    limits = PromoLimits(
        valid_until=until, max_activations=3, only_without_payments=True, tariff_ids=(tariff.id,)
    )
    await update_limits(db_session, promo.id, limits, member_id=owner.id)

    [view] = await list_promos(db_session, NOW)
    assert (promo.valid_until, promo.max_activations, promo.only_without_payments) == (
        until,
        3,
        True,
    )
    assert view.tariff_ids == [tariff.id]
    assert (promo.code, promo.kind, promo.discount_percent) == (
        "SALE15",
        PromoKind.PERCENT_DISCOUNT,
        15,
    )
    actions = list(
        await db_session.scalars(
            select(JournalEntry.action).where(
                journaled_in_test(), JournalEntry.subject_type == "promo_code"
            )
        )
    )
    assert actions == ["promo.created", "promo.limits_changed"]


async def test_8_3_activations_and_disable_enable(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.3: владелец видит число активаций; отключённый промокод ведёт себя как
    несуществующий; его можно включить снова."""
    promo = await _promo(db_session, owner)
    await _activate(db_session, promo, await make_client(db_session, telegram_id=1))
    [view] = await list_promos(db_session, NOW)
    assert (view.activations, view.state) == (1, PromoState.ACTIVE)

    await set_disabled(db_session, promo.id, True, now=NOW, member_id=owner.id)
    [view] = await list_promos(db_session, NOW)
    assert view.state == PromoState.DISABLED
    client = await make_client(db_session, telegram_id=2)
    assert await _rejected_discount(db_session, client, "sale15", tariff) == Rejection.INVALID

    await set_disabled(db_session, promo.id, False, now=NOW, member_id=owner.id)
    assert await enter_discount(db_session, client, "sale15", tariff, NOW) is promo


# --- Скидка (8.4) ---


@pytest.mark.parametrize(
    ("price", "percent", "minimum", "total"),
    [
        # 0065: 15% от 199 ₽ — скидка 29 ₽, итог 170 ₽
        ("199", 15, "1", "170"),
        ("199.00", 15, "1", "170.00"),
        ("199", 100, "1", "0"),
        ("199", None, "1", "199"),
        # Итог больше нуля, но меньше минимума провайдера — поднимается до минимума
        ("10", 95, "5", "5"),
        ("10", 95, "1", "1"),
    ],
)
def test_8_4_discount_rounds_down_to_whole_units(
    price: str, percent: int | None, minimum: str, total: str
) -> None:
    """8.4: скидка округляется вниз до целой единицы валюты; итог ниже минимальной
    суммы счёта поднимается до неё; скидка 100% — ноль (8.8)."""
    assert discounted_total(Decimal(price), percent, Decimal(minimum)) == Decimal(total)


# --- Клиент без оплат (8.2) ---


async def test_8_2_client_without_payments(db_session: AsyncSession, tariff: Tariff) -> None:
    """8.2, 0065: без оплат — нет применённых платежей (нулевой считается) и клиент не
    перенесён с платной подпиской; клиент только с триалом — без оплат."""
    fresh = await make_client(db_session, telegram_id=1)
    assert await without_payments(db_session, fresh)

    trial_only = await make_client(db_session, telegram_id=2)
    trial = make_subscription(trial_only, None, panel_user_id=2)
    trial.is_trial = True
    await add(db_session, trial)
    assert await without_payments(db_session, trial_only)

    free = await make_client(db_session, telegram_id=3)
    zero = make_payment(
        free,
        tariff,
        state=PaymentState.APPLIED,
        applied_at=NOW,
        amount=Decimal(0),
        provider=None,
        provider_payment_id=None,
    )
    await add(db_session, zero)
    assert not await without_payments(db_session, free)

    pending = await make_client(db_session, telegram_id=4)
    await add(db_session, make_payment(pending, tariff))
    assert await without_payments(db_session, pending)


async def test_8_2_migrated_client_by_old_bot_payments(
    db_session: AsyncSession, tariff: Tariff
) -> None:
    """8.2, 0070: перенесённый клиент «с платной подпиской» — по полю файла «Платил в
    старом боте»; без поля усыновлённый считается платившим, перенесённый из файла без
    пользователя в панели — без оплат; своя оплата делает платившим всегда."""
    adopted = await make_client(db_session, telegram_id=5)
    adopted.adopted_at = NOW
    await add(db_session, make_subscription(adopted, None, panel_user_id=5))
    assert not await without_payments(db_session, adopted)

    adopted_new = await make_client(db_session, telegram_id=6)
    adopted_new.adopted_at = NOW
    adopted_new.old_bot_paid = False
    await add(db_session, make_subscription(adopted_new, None, panel_user_id=6))
    assert await without_payments(db_session, adopted_new)

    adopted_paid = await make_client(db_session, telegram_id=7)
    adopted_paid.adopted_at = NOW
    adopted_paid.old_bot_paid = True
    assert not await without_payments(db_session, adopted_paid)

    imported = await make_client(db_session, telegram_id=8)
    imported.imported_at = NOW
    assert await without_payments(db_session, imported)
    imported.old_bot_paid = True
    assert not await without_payments(db_session, imported)

    paid_here = await make_client(db_session, telegram_id=9)
    paid_here.adopted_at = NOW
    paid_here.old_bot_paid = False
    await add(
        db_session,
        make_payment(paid_here, tariff, state=PaymentState.APPLIED, applied_at=NOW),
    )
    assert not await without_payments(db_session, paid_here)


# --- Ошибки (8.12, 8.9, 0066) ---


async def test_8_12_neutral_text_for_missing_disabled_expired_exhausted(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.12: кода нет, промокод отключён, истёк или исчерпан — один нейтральный ответ."""
    client = await make_client(db_session, telegram_id=1)
    disabled = await _promo(db_session, owner, code="OFF")
    await set_disabled(db_session, disabled.id, True, now=NOW, member_id=owner.id)
    await _promo(db_session, owner, code="OLD", valid_until=NOW - timedelta(minutes=1))
    exhausted = await _promo(db_session, owner, code="ONCE", max_activations=1)
    await _activate(db_session, exhausted, await make_client(db_session, telegram_id=2))

    for code in ("NOPE", "OFF", "OLD", "ONCE"):
        assert await _rejected_discount(db_session, client, code, tariff) == Rejection.INVALID


async def test_8_12_named_reasons(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.12: код уже использован клиентом, не подходит к тарифу или только для клиентов
    без оплат — клиент видит причину."""
    client = await make_client(db_session, telegram_id=1)
    used = await _promo(db_session, owner, code="USED")
    await _activate(db_session, used, client)
    other = make_tariff(name="Год")
    await add(db_session, other)
    await _promo(db_session, owner, code="YEAR", tariff_ids=(other.id,))
    await _promo(db_session, owner, code="NEW", only_without_payments=True)

    assert await _rejected_discount(db_session, client, "used", tariff) == Rejection.ALREADY_USED
    assert await _rejected_discount(db_session, client, "year", tariff) == Rejection.WRONG_TARIFF
    # У клиента уже есть применённый платёж (_activate)
    assert await _rejected_discount(db_session, client, "new", tariff) == Rejection.NEW_ONLY
    assert await enter_discount(db_session, client, "year", other, NOW) is not None


async def test_8_9_code_in_wrong_place(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.9, 0066: код на дни на экране подтверждения и код на скидку в меню —
    подсказки, а не неверные попытки (8.13)."""
    client = await make_client(db_session, telegram_id=1)
    await _promo(db_session, owner, code="DAYS7", kind=PromoKind.DAYS, value=7)
    await _promo(db_session, owner, code="SALE")
    for _ in range(10):
        assert (
            await _rejected_discount(db_session, client, "days7", tariff)
            == Rejection.DAYS_ON_CONFIRM
        )
        with pytest.raises(PromoRejectedError) as raised:
            await enter_days(db_session, client, "sale", NOW)
        assert raised.value.reason == Rejection.DISCOUNT_IN_MENU
    assert await blocked_minutes(db_session, client.id, NOW) == 0


async def test_8_11_days_without_subscription(db_session: AsyncSession, owner: TeamMember) -> None:
    """8.11: у клиента нет подписок, к которым можно добавить дни, — код не активируется."""
    client = await make_client(db_session, telegram_id=1)
    await _promo(db_session, owner, code="DAYS7", kind=PromoKind.DAYS, value=7)
    disabled = make_subscription(client, None)
    disabled.expires_at = NOW + timedelta(days=3)
    disabled.panel_status = PanelUserStatus.DISABLED
    await add(db_session, disabled)
    with pytest.raises(PromoRejectedError) as raised:
        await enter_days(db_session, client, "days7", NOW)
    assert raised.value.reason == Rejection.NO_SUBSCRIPTION


# --- Защита от перебора (8.13) ---


async def test_8_13_pause_after_wrong_codes_in_a_row(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.13, 04-operator-settings: после 5 неверных кодов подряд — пауза 15 минут;
    даже верный код в паузу не принимается. Пауза — в журнал."""
    client = await make_client(db_session, telegram_id=1)
    await _promo(db_session, owner)
    for _ in range(5):
        assert await _rejected_discount(db_session, client, "wrong", tariff) == Rejection.INVALID

    with pytest.raises(PromoRejectedError) as raised:
        await enter_discount(db_session, client, "sale15", tariff, NOW)
    assert raised.value.reason == Rejection.RATE_LIMITED
    assert raised.value.minutes == 15
    assert await blocked_minutes(db_session, client.id, NOW + timedelta(minutes=14)) == 1
    assert await blocked_minutes(db_session, client.id, NOW + timedelta(minutes=15)) == 0
    later = NOW + timedelta(minutes=16)
    assert await enter_discount(db_session, client, "sale15", tariff, later) is not None
    limited = await db_session.scalar(
        select(JournalEntry).where(
            journaled_in_test(), JournalEntry.action == "promo.input_limited"
        )
    )
    assert limited is not None


async def test_8_13_success_resets_and_named_reasons_do_not_count(
    db_session: AsyncSession, owner: TeamMember, tariff: Tariff
) -> None:
    """8.13, 0065: неверные — только попытки с нейтральным текстом; удачный ввод
    сбрасывает счётчик."""
    client = await make_client(db_session, telegram_id=1)
    await _promo(db_session, owner)
    other = make_tariff(name="Год")
    await add(db_session, other)
    await _promo(db_session, owner, code="YEAR", tariff_ids=(other.id,))
    for _ in range(4):
        await _rejected_discount(db_session, client, "wrong", tariff)
    await enter_discount(db_session, client, "sale15", tariff, NOW)
    for _ in range(4):
        await _rejected_discount(db_session, client, "wrong", tariff)
    for _ in range(3):
        assert (
            await _rejected_discount(db_session, client, "year", tariff) == Rejection.WRONG_TARIFF
        )
    assert await blocked_minutes(db_session, client.id, NOW) == 0


# --- API админки (А7, «Настройки → Промокоды») ---


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


async def _sign_in(shop: Shop, role: TeamRole) -> TeamMember:
    member = TeamMember(telegram_id=10 if role == TeamRole.OWNER else 11, role=role)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


async def test_8_1_8_2_owner_creates_promo_via_api(shop: Shop, tariff: Tariff) -> None:
    """8.1, 8.2: владелец создаёт промокод с ограничениями; повтор кода в другом
    регистре — отказ."""
    await _sign_in(shop, TeamRole.OWNER)
    body = {
        "code": "sale10",
        "kind": "percent_discount",
        "value": 10,
        "max_activations": 100,
        "only_without_payments": True,
        "tariff_ids": [tariff.id],
        "valid_until": "2026-12-31",
    }
    response = await shop.http.post(f"{API}/promo-codes", json=body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["code"], created["value"], created["state"]) == ("SALE10", 10, "active")
    assert created["tariff_ids"] == [tariff.id]
    # Последний день — включительно, в часовом поясе магазина (по умолчанию московском)
    assert created["valid_until"] == "2026-12-31"
    promo = await shop.session.get(PromoCode, created["id"])
    assert promo is not None
    assert promo.valid_until == datetime(2026, 12, 31, 21, 0, tzinfo=UTC)

    again = await shop.http.post(f"{API}/promo-codes", json={**body, "code": "Sale10"})
    assert again.status_code == 409

    generated = await shop.http.get(f"{API}/promo-codes/generate")
    assert len(generated.json()["code"]) == 8

    listed = (await shop.http.get(f"{API}/promo-codes")).json()
    assert [p["code"] for p in listed["promo_codes"]] == ["SALE10"]
    assert tariff.id in [t["id"] for t in listed["tariffs"]]


@pytest.mark.parametrize("value", [0, 101])
async def test_8_1_api_rejects_percent_out_of_range(shop: Shop, value: int) -> None:
    """8.1: скидка — от 1 до 100%."""
    await _sign_in(shop, TeamRole.OWNER)
    body = {"code": "X", "kind": "percent_discount", "value": value}
    response = await shop.http.post(f"{API}/promo-codes", json=body)
    assert response.status_code == 422


async def test_8_16_api_changes_limits_but_not_code(shop: Shop, owner: TeamMember) -> None:
    """8.16: через API меняются только ограничения; код, вид и значение — нет."""
    promo = await _promo(shop.session, owner)
    await _sign_in(shop, TeamRole.OWNER)
    body = {"max_activations": 5, "code": "OTHER", "kind": "days", "value": 99}
    response = await shop.http.put(f"{API}/promo-codes/{promo.id}/limits", json=body)
    assert response.status_code == 200, response.text
    changed = response.json()
    assert (changed["code"], changed["kind"], changed["value"]) == (
        "SALE15",
        "percent_discount",
        15,
    )
    assert changed["max_activations"] == 5


async def test_8_3_owner_disables_and_enables(shop: Shop, owner: TeamMember) -> None:
    """8.3: отключение и повторное включение."""
    promo = await _promo(shop.session, owner)
    await _sign_in(shop, TeamRole.OWNER)
    disabled = await shop.http.post(f"{API}/promo-codes/{promo.id}/disable")
    assert disabled.json()["state"] == "disabled"
    enabled = await shop.http.post(f"{API}/promo-codes/{promo.id}/enable")
    assert enabled.json()["state"] == "active"


async def test_8_3_assistant_sees_but_does_not_change(shop: Shop, owner: TeamMember) -> None:
    """8.3, 0065: помощник видит промокоды и активации, но не меняет их."""
    promo = await _promo(shop.session, owner)
    client = await make_client(shop.session, telegram_id=777)
    await _activate(shop.session, promo, client)
    await _sign_in(shop, TeamRole.ASSISTANT)

    listed = await shop.http.get(f"{API}/promo-codes")
    assert listed.status_code == 200
    assert listed.json()["promo_codes"][0]["activations"] == 1
    activations = await shop.http.get(f"{API}/promo-codes/{promo.id}/activations")
    assert activations.status_code == 200
    [activation] = activations.json()
    assert activation["client"]["telegram_id"] == 777

    body = {"code": "NEW", "kind": "days", "value": 3}
    assert (await shop.http.post(f"{API}/promo-codes", json=body)).status_code == 403
    assert (await shop.http.get(f"{API}/promo-codes/generate")).status_code == 403
    limits = await shop.http.put(f"{API}/promo-codes/{promo.id}/limits", json={})
    assert limits.status_code == 403
    assert (await shop.http.post(f"{API}/promo-codes/{promo.id}/disable")).status_code == 403
    assert (await shop.http.get(f"{API}/settings/promo")).status_code == 403


async def test_8_13_settings_brute_force_protection(shop: Shop) -> None:
    """8.13, 04-operator-settings: защита от перебора — 5 попыток и 15 минут по
    умолчанию; владелец меняет значения."""
    await _sign_in(shop, TeamRole.OWNER)
    current = (await shop.http.get(f"{API}/settings/promo")).json()
    assert current == {"max_failed_attempts": 5, "pause_minutes": 15}
    body = {"max_failed_attempts": 3, "pause_minutes": 60}
    saved = await shop.http.put(f"{API}/settings/promo", json=body)
    assert saved.json() == body
    rejected = await shop.http.put(
        f"{API}/settings/promo", json={"max_failed_attempts": 0, "pause_minutes": 60}
    )
    assert rejected.status_code == 422


async def test_8_10_deleted_subscription_is_not_a_target(db_session: AsyncSession) -> None:
    """8.10, 4.6: подписка, удалённая в панели, для дней не предлагается."""
    client = await make_client(db_session, telegram_id=1)
    gone = make_subscription(client, None)
    gone.expires_at = NOW + timedelta(days=1)
    gone.deleted_at = NOW
    await add(db_session, gone)
    assert await days_targets(db_session, client.id) == []
