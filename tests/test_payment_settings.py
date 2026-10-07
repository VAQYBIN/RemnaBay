"""«Настройки → Оплата»: подключение ЮКассы, время жизни счёта (3.8, 3.31, 3.33, 1.14, 0049)."""

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.crypto import SecretBox, generate_key
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.payments import INVOICE_LIFETIME, Providers
from remnabay.payments.yookassa import YOOKASSA, YooKassaKind
from remnabay.shop import SHOP_CURRENCY
from remnabay.shop_settings import get_setting
from tests.domain_support import add, make_team_member, put_setting
from tests.payment_support import (
    YOOKASSA_SECRET,
    YOOKASSA_SHOP_ID,
    FakeYooKassa,
    connect_yookassa,
    shop_box,
)
from tests.web_support import Shop, running_shop

SETTINGS = "/api/admin/settings/payment"
KEYS = f"{SETTINGS}/providers/{YOOKASSA}"
VALID = {"credentials": {"shop_id": YOOKASSA_SHOP_ID, "secret_key": YOOKASSA_SECRET}}


@pytest.fixture
def yookassa() -> FakeYooKassa:
    return FakeYooKassa()


@pytest.fixture
async def shop(
    valid_env: dict[str, str], db_session: AsyncSession, yookassa: FakeYooKassa
) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop, yookassa.http() as http:
        shop.use_providers(Providers(shop_box(), [YooKassaKind(http)]))
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = make_team_member(telegram_id=500, role=TeamRole.OWNER)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


def _yookassa(body: dict[str, Any]) -> dict[str, Any]:
    providers: list[dict[str, Any]] = body["providers"]
    [provider] = [p for p in providers if p["code"] == YOOKASSA]
    return provider


async def _payment_item(shop: Shop) -> str:
    items: list[dict[str, Any]] = (await shop.http.get("/api/admin/checklist")).json()["items"]
    [item] = [i for i in items if i["key"] == "payment"]
    status: str = item["status"]
    return status


@pytest.mark.usefixtures("owner")
async def test_3_31_yookassa_is_offered_with_webhook_address(shop: Shop) -> None:
    """3.31, 3.28: ЮКасса доступна для подключения; адрес вебхука — для кабинета ЮКассы."""
    body = (await shop.http.get(SETTINGS)).json()
    provider = _yookassa(body)
    assert provider["state"] == "not_connected"
    assert provider["webhook_url"] == f"https://shop.example.com/webhooks/payments/{YOOKASSA}"
    assert body["invoice_lifetime_minutes"] == 30


@pytest.mark.usefixtures("owner")
async def test_1_14_connected_yookassa_completes_payment_item(shop: Shop) -> None:
    """1.14, 3.31: ключи проверены у ЮКассы и сохранены — способ оплаты подключён;
    ключи в ответе не показываются."""
    assert await _payment_item(shop) == "todo"
    response = await shop.http.put(KEYS, json=VALID)

    assert response.status_code == 200
    assert _yookassa(response.json())["state"] == "ready"
    assert YOOKASSA_SECRET not in response.text
    assert await _payment_item(shop) == "done"


@pytest.mark.usefixtures("owner")
async def test_keys_rejected_by_yookassa_are_not_saved(shop: Shop) -> None:
    """Ключи, которые ЮКасса не приняла, не сохраняются; админка объясняет почему."""
    response = await shop.http.put(
        KEYS, json={"credentials": {"shop_id": "1", "secret_key": "wrong"}}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "rejected"
    assert _yookassa((await shop.http.get(SETTINGS)).json())["state"] == "not_connected"


@pytest.mark.usefixtures("owner")
async def test_malformed_keys_are_rejected(shop: Shop) -> None:
    """Идентификатор магазина ЮКассы — число."""
    response = await shop.http.put(
        KEYS, json={"credentials": {"shop_id": "shop", "secret_key": "x"}}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "invalid_keys"


@pytest.mark.usefixtures("owner")
async def test_yookassa_unavailable_while_connecting(shop: Shop, yookassa: FakeYooKassa) -> None:
    """ЮКасса не отвечает — ключи не проверить, подключение откладывается."""
    yookassa.down = True
    response = await shop.http.put(KEYS, json=VALID)
    assert response.status_code == 503


@pytest.mark.usefixtures("owner")
async def test_3_33_provider_without_shop_currency_is_shown_unavailable(shop: Shop) -> None:
    """3.33: провайдер не принимает валюту учёта — админка показывает это при подключении,
    а способ оплаты не считается подключённым."""
    await put_setting(shop.session, SHOP_CURRENCY, "USD")
    response = await shop.http.put(KEYS, json=VALID)

    assert _yookassa(response.json())["state"] == "currency_unsupported"
    assert _yookassa(response.json())["currencies"] == ["RUB"]
    assert await _payment_item(shop) == "todo"


@pytest.mark.usefixtures("owner")
async def test_0049_keys_need_to_be_entered_again_after_key_change(shop: Shop) -> None:
    """0049: ключи не расшифровываются — «Ключи нужно ввести заново», пункт чек-листа
    «Способ оплаты» снова не выполнен."""
    await connect_yookassa(shop.session, SecretBox(generate_key()))
    assert _yookassa((await shop.http.get(SETTINGS)).json())["state"] == "keys_lost"
    assert await _payment_item(shop) == "todo"


@pytest.mark.usefixtures("owner")
async def test_disconnect_provider(shop: Shop) -> None:
    await shop.http.put(KEYS, json=VALID)
    response = await shop.http.delete(KEYS)
    assert _yookassa(response.json())["state"] == "not_connected"


@pytest.mark.usefixtures("owner")
async def test_3_8_invoice_lifetime_is_set_in_admin(shop: Shop) -> None:
    """3.8: время жизни счёта задаёт оператор."""
    response = await shop.http.put(SETTINGS, json={"invoice_lifetime_minutes": 45})
    assert response.json()["invoice_lifetime_minutes"] == 45
    assert await get_setting(shop.session, INVOICE_LIFETIME) == timedelta(minutes=45)


async def test_payment_settings_are_for_owner_only(shop: Shop) -> None:
    """Настройки платёжек — только владелец (01-domain)."""
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)
    await shop.sign_in(assistant)
    assert (await shop.http.get(SETTINGS)).status_code == 403
    assert (await shop.http.put(KEYS, json=VALID)).status_code == 403
