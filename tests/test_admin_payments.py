"""«Требуют внимания» и карточка платежа в API админки (А4, А5; 4.20–4.23, 4.30–4.32)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.tariffs import TariffState
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.messaging import SendArgs
from remnabay.payments import TariffSnapshot, apply_payment
from remnabay.queue import AttemptResult, QueueAttempt, TaskRegistry, TaskStatus
from remnabay.queue._models import QueueTask
from remnabay.web.admin import _payments
from tests.domain_support import add, make_client, make_payment, make_subscription, make_tariff
from tests.queue_support import irreversible
from tests.web_support import Shop, running_shop

API = "/api/admin"


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def member(shop: Shop) -> TeamMember:
    """Разбирать платежи может и помощник (01-domain, таблица ролей)."""
    await add(shop.session, TeamMember(telegram_id=1, role=TeamRole.OWNER))
    assistant = TeamMember(telegram_id=2, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)
    return assistant


async def _not_applied(shop: Shop, **overrides: Any) -> Payment:
    client = await make_client(shop.session, telegram_id=777)
    tariff = make_tariff()
    await add(shop.session, tariff)
    subscription = make_subscription(client, tariff, panel_user_id=7)
    await add(shop.session, subscription)
    values: dict[str, Any] = {
        "state": PaymentState.PAID_NOT_APPLIED,
        "purpose": PaymentPurpose.RENEWAL,
        "subscription_id": subscription.id,
        "tariff_snapshot": TariffSnapshot.of(tariff).stored(),
        "paid_at": datetime.now(UTC),
    }
    values.update(overrides)
    payment = make_payment(client, tariff, **values)
    await add(shop.session, payment)
    return payment


async def _failed_apply(shop: Shop, payment: Payment, error: str = "Панель ответила 500") -> int:
    task = QueueTask(
        name=apply_payment.name,
        args={"payment_id": payment.id},
        key=f"subscription:{payment.subscription_id}",
        status=TaskStatus.FAILED,
    )
    await add(shop.session, task)
    await add(
        shop.session,
        QueueAttempt(
            task_id=task.id,
            number=1,
            retry_round=1,
            finished_at=datetime.now(UTC),
            result=AttemptResult.ERROR,
            error=error,
        ),
    )
    return task.id


async def _messages(shop: Shop) -> list[SendArgs]:
    rows = await shop.session.scalars(
        select(QueueTask.args).where(QueueTask.name == "messages.send").order_by(QueueTask.id)
    )
    return [SendArgs.model_validate(args) for args in rows]


# --- «Требуют внимания» ---


@pytest.mark.usefixtures("member")
async def test_4_20_not_applied_payment_is_in_attention(shop: Shop) -> None:
    """4.20: «оплачен — не применён» — в «Требуют внимания», в счётчике."""
    payment = await _not_applied(shop)
    body = (await shop.http.get(f"{API}/attention")).json()

    assert body["count"] == 1
    [row] = body["payments"]
    assert (row["id"], row["state"], row["client"]["telegram_id"]) == (
        payment.id,
        "paid_not_applied",
        777,
    )
    overview = (await shop.http.get(f"{API}/overview")).json()
    assert overview["attention"] == 1


@pytest.mark.usefixtures("member")
async def test_3_39_amount_mismatch_is_marked(shop: Shop) -> None:
    """3.39: в «Требуют внимания» видна пометка «сумма не совпала»."""
    await _not_applied(shop, paid_amount=Decimal("150.00"), paid_currency="RUB")
    [row] = (await shop.http.get(f"{API}/attention")).json()["payments"]
    assert row["amount_mismatch"] is True


@pytest.mark.usefixtures("member")
async def test_4_30_waiting_panel_is_listed_separately(shop: Shop) -> None:
    """4.30: операции «ждёт панель» видны отдельно и в счётчик не входят."""
    payment = await _not_applied(shop, state=PaymentState.PAID)
    await add(
        shop.session,
        QueueTask(
            name=apply_payment.name,
            args={"payment_id": payment.id},
            status=TaskStatus.WAITING_PANEL,
        ),
    )
    body = (await shop.http.get(f"{API}/attention")).json()
    assert body["count"] == 0
    [waiting] = body["waiting_panel"]
    assert waiting["payment_id"] == payment.id


# --- Карточка платежа ---


@pytest.mark.usefixtures("member")
async def test_4_21_card_shows_client_conditions_attempts_and_error(shop: Shop) -> None:
    """4.21: карточка — клиент, что должно было примениться, история попыток, ошибка."""
    payment = await _not_applied(shop)
    await _failed_apply(shop, payment, "Пользователь не найден")
    body = (await shop.http.get(f"{API}/payments/{payment.id}")).json()

    assert body["payment"]["client"]["telegram_id"] == 777
    assert body["snapshot"]["name"] == "Месяц"
    assert body["snapshot"]["duration_days"] == 30
    assert body["subscription"]["panel_username"] == "user_7"
    [attempt] = body["attempts"]
    assert attempt["error"] == "Пользователь не найден"
    assert body["last_error"] == "Пользователь не найден"
    assert body["actions"] == ["retry", "apply_as_new", "resolve"]


@pytest.mark.usefixtures("member")
async def test_4_22_retry_applies_payment_again(shop: Shop) -> None:
    """4.22: «Применить повторно» — платёж снова «оплачен», применение — новым кругом."""
    payment = await _not_applied(shop)
    task_id = await _failed_apply(shop, payment)
    response = await shop.http.post(f"{API}/payments/{payment.id}/retry")

    assert response.status_code == 200
    assert payment.state == PaymentState.PAID
    task = await shop.session.get(QueueTask, task_id)
    assert task is not None
    assert task.status == TaskStatus.PENDING


@pytest.mark.usefixtures("member")
async def test_4_22_apply_as_new_subscription(shop: Shop) -> None:
    """4.22: «Применить созданием новой подписки» — покупка новой подписки."""
    payment = await _not_applied(shop)
    await _failed_apply(shop, payment)
    response = await shop.http.post(f"{API}/payments/{payment.id}/apply-as-new")

    assert response.status_code == 200
    assert (payment.purpose, payment.subscription_id, payment.new_subscription) == (
        PaymentPurpose.PURCHASE,
        None,
        True,
    )


@pytest.mark.usefixtures("member")
async def test_4_22_resolve_manually_needs_comment_and_tells_client(shop: Shop) -> None:
    """4.22, 4.24: «Отметить решённым вручную» — с обязательным комментарием; клиент
    получает нейтральное уведомление, комментарий виден только команде."""
    payment = await _not_applied(shop)
    empty = await shop.http.post(f"{API}/payments/{payment.id}/resolve", json={"comment": ""})
    assert empty.status_code == 422

    response = await shop.http.post(
        f"{API}/payments/{payment.id}/resolve", json={"comment": "Продлил руками в панели"}
    )
    assert response.status_code == 200
    assert payment.state == PaymentState.RESOLVED_MANUALLY
    [message] = await _messages(shop)
    assert message.text_key == "event.payment_resolved.manual"
    assert "Продлил" not in str(message.variables)
    history = [entry["action"] for entry in response.json()["history"]]
    assert "payment.resolved_manually" in history


@pytest.mark.usefixtures("member")
async def test_4_22_actions_only_for_not_applied_payment(shop: Shop) -> None:
    """4.22: разбирают только «оплачен — не применён»; у применённого действий нет."""
    payment = await _not_applied(shop, state=PaymentState.APPLIED)
    card = (await shop.http.get(f"{API}/payments/{payment.id}")).json()
    assert card["actions"] == []
    response = await shop.http.post(f"{API}/payments/{payment.id}/retry")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_available"


# --- Неизвестный платёж ---


async def _unknown(shop: Shop) -> Payment:
    payment = Payment(
        is_unknown=True,
        state=PaymentState.PAID_NOT_APPLIED,
        amount=Decimal("199.00"),
        currency="RUB",
        paid_amount=Decimal("199.00"),
        paid_currency="RUB",
        provider="yookassa",
        provider_payment_id="old-bot-1",
        paid_at=datetime.now(UTC),
    )
    await add(shop.session, payment)
    return payment


@pytest.mark.usefixtures("member")
async def test_4_23_unknown_payment_is_bound_to_client_as_purchase(shop: Shop) -> None:
    """4.23: неизвестный платёж — в «Требуют внимания» с пометкой; команда находит
    клиента по Telegram ID, привязывает платёж и выбирает, что применить."""
    payment = await _unknown(shop)
    client = await make_client(shop.session, telegram_id=888)
    tariff = make_tariff(name="Год", duration_days=365)
    await add(shop.session, tariff)

    [row] = (await shop.http.get(f"{API}/attention")).json()["payments"]
    assert row["is_unknown"] is True
    assert (await shop.http.get(f"{API}/payments/{payment.id}")).json()["actions"] == [
        "bind",
        "resolve",
    ]
    lookup = (await shop.http.get(f"{API}/clients/by-telegram/888")).json()
    assert lookup["client"]["id"] == client.id

    response = await shop.http.post(
        f"{API}/payments/{payment.id}/bind",
        json={"telegram_id": 888, "purpose": "purchase", "tariff_id": tariff.id},
    )
    assert response.status_code == 200
    assert (payment.client_id, payment.state, payment.tariff_id) == (
        client.id,
        PaymentState.PAID,
        tariff.id,
    )
    assert TariffSnapshot.of_payment(payment).duration_days == 365
    names = list(await shop.session.scalars(select(QueueTask.name)))
    assert apply_payment.name in names


@pytest.mark.usefixtures("member")
async def test_4_23_bind_to_unknown_telegram_id_is_rejected(shop: Shop) -> None:
    payment = await _unknown(shop)
    tariff = make_tariff()
    await add(shop.session, tariff)
    response = await shop.http.post(
        f"{API}/payments/{payment.id}/bind",
        json={"telegram_id": 999, "purpose": "purchase", "tariff_id": tariff.id},
    )
    assert response.status_code == 409


# --- Список платежей ---


@pytest.mark.usefixtures("member")
async def test_a4_payments_filtered_by_state_and_period(shop: Shop) -> None:
    """А4: список платежей с фильтром по состоянию (отдельно — неизвестные) и периоду."""
    applied = await _not_applied(shop, state=PaymentState.APPLIED)
    unknown = await _unknown(shop)
    old = make_payment(None, None, is_unknown=True, state=PaymentState.PAID_NOT_APPLIED)
    old.created_at = datetime.now(UTC) - timedelta(days=40)
    old.provider_payment_id = "old-2"
    await add(shop.session, old)

    by_state = (await shop.http.get(f"{API}/payments", params={"state": "applied"})).json()[
        "payments"
    ]
    assert [row["id"] for row in by_state] == [applied.id]
    unknowns = (await shop.http.get(f"{API}/payments", params={"state": "unknown"})).json()[
        "payments"
    ]
    assert [row["id"] for row in unknowns] == [unknown.id, old.id]
    since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    recent = (await shop.http.get(f"{API}/payments", params={"since": since})).json()["payments"]
    assert old.id not in [row["id"] for row in recent]


# --- Проваленные операции ---


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> TaskRegistry:
    tasks = TaskRegistry((apply_payment, irreversible))
    monkeypatch.setattr(_payments, "TASKS", tasks)
    return tasks


async def _failed_operation(shop: Shop, name: str = "grant.days") -> QueueTask:
    task = QueueTask(name=name, args={"label": "x"}, key="subscription:5", status=TaskStatus.FAILED)
    behind = QueueTask(name="other", args={}, key="subscription:5", status=TaskStatus.PENDING)
    await add(shop.session, task, behind)
    return task


@pytest.mark.usefixtures("member", "registry")
async def test_4_31_failed_operations_are_listed_with_waiting_behind(shop: Shop) -> None:
    """4.31, 4.27: проваленные операции, а не только платежи; видно, сколько ждёт за ней."""
    task = await _failed_operation(shop)
    body = (await shop.http.get(f"{API}/attention")).json()
    [row] = body["operations"]
    assert (row["task_id"], row["waiting_behind"], row["cancellable"]) == (task.id, 1, True)


@pytest.mark.usefixtures("member", "registry")
async def test_4_31_4_32_cancel_operation_with_comment(shop: Shop) -> None:
    """4.31, 4.32: «Отменить» с комментарием — операция снята, следующие операции
    подписки идут дальше."""
    task = await _failed_operation(shop)
    empty = await shop.http.post(f"{API}/operations/{task.id}/cancel", json={"comment": " "})
    assert empty.status_code == 409

    response = await shop.http.post(
        f"{API}/operations/{task.id}/cancel", json={"comment": "Клиент передумал"}
    )
    assert response.status_code == 200
    await shop.session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    assert response.json()["operations"] == []


@pytest.mark.usefixtures("member", "registry")
async def test_4_31_irreversible_operation_cannot_be_cancelled(shop: Shop) -> None:
    """4.31: смену даты при возврате отменить нельзя — только повторить или отметить
    решённой вручную."""
    task = await _failed_operation(shop, name=irreversible.name)
    [row] = (await shop.http.get(f"{API}/attention")).json()["operations"]
    assert row["cancellable"] is False
    response = await shop.http.post(
        f"{API}/operations/{task.id}/cancel", json={"comment": "Отмена"}
    )
    assert response.status_code == 409

    resolved = await shop.http.post(
        f"{API}/operations/{task.id}/resolve", json={"comment": "Дату поправил вручную"}
    )
    assert resolved.status_code == 200


@pytest.mark.usefixtures("member", "registry")
async def test_4_31_retry_operation(shop: Shop) -> None:
    """4.31: «Повторить» — операция снова в очереди."""
    task = await _failed_operation(shop)
    response = await shop.http.post(f"{API}/operations/{task.id}/retry")
    assert response.status_code == 200
    await shop.session.refresh(task)
    assert task.status == TaskStatus.PENDING


async def test_attention_needs_sign_in(shop: Shop) -> None:
    assert (await shop.http.get(f"{API}/attention")).status_code == 401


@pytest.mark.usefixtures("member")
async def test_4_23_tariff_options_for_binding(shop: Shop) -> None:
    """4.23: для привязки неизвестного платежа помощник видит тарифы в продаже и в архиве."""
    on_sale = make_tariff(name="Месяц")
    archived = make_tariff(name="Старый", state=TariffState.ARCHIVED)
    closed = make_tariff(name="Закрытый", state=TariffState.CLOSED)
    await add(shop.session, on_sale, archived, closed)
    names = [row["name"] for row in (await shop.http.get(f"{API}/tariff-options")).json()]
    assert names == ["Месяц", "Старый"]
