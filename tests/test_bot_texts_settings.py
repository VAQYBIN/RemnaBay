"""«Настройки → Тексты бота» (1.29): тексты по экранам, правка, предпросмотр, сброс."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.team import TeamMember, TeamRole
from remnabay.journal import JournalEntry
from remnabay.messaging import SendArgs, load_texts, send_message
from remnabay.panel_users import default_name
from remnabay.queue import TaskContext
from remnabay.shop import SHOP_STATE, ShopState
from remnabay.texts import (
    DATE_MARK_START,
    GROUPS,
    BotTextOverride,
    group_of,
    load_default_catalogs,
)
from tests.bot_support import telegram_user
from tests.bot_texts_doc import default_headings
from tests.conftest import journaled_in_test
from tests.domain_support import add, make_client, make_team_member, put_setting
from tests.panel_support import FakeSender, fake_runtime
from tests.web_support import Shop, running_shop

TEXTS = "/api/admin/settings/texts"
BRAND = "/api/admin/settings/brand"
UNKNOWN = "Не совсем понял. Откройте главное меню — там всё самое нужное."


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.fixture
async def owner(shop: Shop) -> TeamMember:
    member = make_team_member(telegram_id=500, role=TeamRole.OWNER)
    await add(shop.session, member)
    await shop.sign_in(member)
    return member


def _items(body: Any) -> dict[str, Any]:
    """Тексты ответа по ключу, из всех групп."""
    return {item["key"]: item for group in body["groups"] for item in group["texts"]}


def _title(heading: str) -> str:
    # «— rich» в документе — пометка для разработки, владелец видит только название экрана
    return heading.removesuffix(" — rich")


def test_1_29_groups_follow_bot_texts_spec() -> None:
    """1.29: тексты сгруппированы по экранам так же, как в 03-bot-texts.md: каждый текст —
    в группе своего раздела, группы — в порядке документа."""
    spec = default_headings()

    for key, heading in spec.items():
        group = group_of(key)
        assert (group.title, group.section) == (_title(heading.title), heading.section), key
    order = list(dict.fromkeys((_title(h.title), h.section) for h in spec.values()))
    assert [(group.title, group.section) for group in GROUPS] == order


def test_1_29_every_text_has_a_group() -> None:
    """1.29: ни один текст не потерян — у каждого ключа ru.toml, включая формы
    множественного числа, есть группа."""
    catalog = load_default_catalogs()["ru"]

    assert catalog.entries.keys() >= catalog.texts.keys()
    for key in catalog.entries:
        group_of(key)


async def test_1_29_list_shows_all_texts_by_screens(shop: Shop, owner: TeamMember) -> None:
    """1.29: владелец видит все тексты по экранам: текст по умолчанию, текущий, пометку
    «изменён» и допустимые переменные. Скрыты только формы множественного числа, которые
    для целых чисел не используются (у русского — `other`)."""
    del owner
    await add(shop.session, BotTextOverride(key="btn.renew", language="ru", text="Продлить сейчас"))
    catalog = load_default_catalogs()["ru"]

    response = await shop.http.get(TEXTS)

    assert response.status_code == 200
    body = response.json()
    assert body["language"] == "ru"
    assert body["languages"] == ["ru"]
    assert [group["id"] for group in body["groups"]] == [group.id for group in GROUPS]
    items = _items(body)
    hidden = set(catalog.entries) - set(items)
    assert hidden == {"unit.days.other", "unit.minutes.other"}
    assert all(not catalog.plural_counts(key) for key in hidden)
    assert items["btn.renew"] == {
        "key": "btn.renew",
        "default": "Продлить",
        "text": "Продлить сейчас",
        "changed": True,
        "variables": [],
        "kind": "button",
        "plural_counts": None,
    }
    renewed = items["event.renewed"]
    assert renewed["changed"] is False
    assert renewed["variables"] == ["subscription_name", "end_date"]
    assert renewed["kind"] == "message"
    assert items["unit.days.few"]["plural_counts"][:4] == [2, 3, 4, 22]
    assert items["command.start"]["kind"] == "command"
    assert items["redirect.title"]["kind"] == "page"


async def test_1_29_save_applies_without_restart_and_is_journaled(
    shop: Shop, owner: TeamMember
) -> None:
    """1.29: изменённый текст бот берёт сразу, без перезапуска; изменение — в журнале."""
    await put_setting(shop.session, SHOP_STATE, ShopState.OPEN)
    await shop.send_text(telegram_user(100), "что-то непонятное")

    response = await shop.http.put(
        f"{TEXTS}/ru/fallback.unknown", json={"text": "  Не понял вас. Загляните в меню.  "}
    )

    assert response.status_code == 200
    assert response.json()["text"] == "Не понял вас. Загляните в меню."
    assert response.json()["changed"] is True
    await shop.send_text(telegram_user(100), "ещё раз")
    assert shop.telegram.texts() == [UNKNOWN, "Не понял вас. Загляните в меню."]
    entry = await shop.session.scalar(
        select(JournalEntry).where(journaled_in_test(), JournalEntry.action == "text.changed")
    )
    assert entry is not None
    assert entry.actor_ref == str(owner.id)
    assert entry.details["key"] == "fallback.unknown"


async def test_1_29_worker_messages_use_saved_text(shop: Shop, owner: TeamMember) -> None:
    """1.29: сообщения из очереди (воркер) собираются с сохранённым текстом: тексты
    читаются из базы при каждой отправке, кэша между процессами нет."""
    del owner
    client = await make_client(shop.session)
    await shop.http.put(
        f"{TEXTS}/ru/event.renewed", json={"text": "«{subscription_name}» продлена. Спасибо!"}
    )
    sender = FakeSender()
    args = SendArgs(
        chat_id=100,
        client_id=client.id,
        text_key="event.renewed",
        variables={"subscription_name": "Основная"},
        dates={"end_date": datetime(2026, 12, 1, tzinfo=UTC)},
    )

    context = TaskContext(session=shop.session, task_id=1, attempt_number=1, previous_result=None)
    with fake_runtime(sender):
        await send_message.run(context, args.model_dump(mode="json"))

    assert [message.text for _, message in sender.sent] == ["«Основная» продлена. Спасибо!"]


async def test_1_29_reset_returns_default(shop: Shop, owner: TeamMember) -> None:
    """1.29: сброс возвращает текст по умолчанию; сброс — в журнале."""
    del owner
    await shop.http.put(f"{TEXTS}/ru/btn.renew", json={"text": "Продлить сейчас"})

    response = await shop.http.delete(f"{TEXTS}/ru/btn.renew")

    assert response.status_code == 200
    assert response.json()["text"] == "Продлить"
    assert response.json()["changed"] is False
    texts = await load_texts(shop.session)
    assert texts.render("btn.renew", "ru") == "Продлить"
    reset = await shop.session.scalar(
        select(JournalEntry).where(journaled_in_test(), JournalEntry.action == "text.reset")
    )
    assert reset is not None


async def test_1_29_saving_default_text_is_reset(shop: Shop, owner: TeamMember) -> None:
    """1.29: текст, совпавший с текстом по умолчанию, не числится изменённым."""
    del owner
    await shop.http.put(f"{TEXTS}/ru/btn.renew", json={"text": "Продлить сейчас"})

    response = await shop.http.put(f"{TEXTS}/ru/btn.renew", json={"text": "Продлить"})

    assert response.json()["changed"] is False
    assert await shop.session.get(BotTextOverride, ("btn.renew", "ru")) is None


@pytest.mark.parametrize(
    ("key", "text", "problem"),
    [
        ("event.renewed", "Продлено за {price}", "нельзя использовать {price}"),
        ("event.renewed", "Подписка {subscription name} продлена", "{subscription name}"),
        ("event.renewed", "Подписка {subscription_name продлена", "{subscription_name продлена"),
        ("event.renewed", "Продлена до {end_date}}", "«}»"),
        ("event.renewed", "   ", "пустой"),
        ("event.renewed", f"Продлена {DATE_MARK_START}0", "служебные символы"),
        ("event.renewed", "а" * 4100, "не больше 4096"),
        ("command.help", "п" * 257, "не больше 256"),
        ("unit.days.few", "{count} дня {days}", "нельзя использовать {days}"),
    ],
)
async def test_1_29_wrong_text_is_not_saved(
    shop: Shop, owner: TeamMember, key: str, text: str, problem: str
) -> None:
    """1.29: текст с ошибкой в переменных или форматировании не сохраняется; админка
    получает понятное объяснение — и в предпросмотре, и при сохранении."""
    del owner

    preview = await shop.http.post(f"{TEXTS}/ru/{key}/preview", json={"text": text})
    saved = await shop.http.put(f"{TEXTS}/ru/{key}", json={"text": text})

    assert preview.status_code == 200
    assert preview.json()["text"] is None
    assert problem in preview.json()["problem"]
    assert saved.status_code == 422
    assert problem in saved.json()["detail"]
    assert await shop.session.get(BotTextOverride, (key, "ru")) is None


async def test_1_29_preview_with_sample_values(shop: Shop, owner: TeamMember) -> None:
    """1.29: предпросмотр — текст так, как его получит клиент: переменные заменены
    примерами из настроек магазина и других текстов бота."""
    del owner
    await shop.http.put(BRAND, json={"name": "Енот VPN", "primary_color": "#7C3AED"})
    await shop.http.put(f"{TEXTS}/ru/subscription.default_name", json={"text": "Домашняя"})

    response = await shop.http.post(
        f"{TEXTS}/ru/menu.header.subscriptions/preview",
        json={"text": "{brand_name}: {subscriptions_summary}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["problem"] is None
    assert body["text"].startswith("Енот VPN: Домашняя — активна до ")
    assert "осталось 30 дней" in body["text"]
    assert await shop.session.get(BotTextOverride, ("menu.header.subscriptions", "ru")) is None


async def test_1_29_every_default_text_passes_preview(shop: Shop, owner: TeamMember) -> None:
    """1.29: тексты по умолчанию проходят ту же проверку: у каждой переменной есть
    пример, каждый текст собирается и укладывается в ограничения Telegram."""
    del owner
    items = _items((await shop.http.get(TEXTS)).json())

    for key, item in items.items():
        response = await shop.http.post(f"{TEXTS}/ru/{key}/preview", json={"text": item["default"]})
        assert response.json()["problem"] is None, key
        assert "{" not in response.json()["text"], key


async def test_1_29_plural_form_override(shop: Shop, owner: TeamMember) -> None:
    """1.29: формы множественного числа правятся по отдельности; число подставляется
    в нужную форму."""
    del owner

    preview = await shop.http.post(
        f"{TEXTS}/ru/unit.days.many/preview", json={"text": "{count} суток"}
    )
    saved = await shop.http.put(f"{TEXTS}/ru/unit.days.many", json={"text": "{count} суток"})

    assert preview.json() == {"text": "0 суток", "problem": None}
    assert saved.status_code == 200
    texts = await load_texts(shop.session)
    assert texts.quantity("unit.days", "ru", 5) == "5 суток"
    assert texts.quantity("unit.days", "ru", 3) == "3 дня"


async def test_1_29_date_fallback_time_zone_label(shop: Shop, owner: TeamMember) -> None:
    """1.29, 0075: подпись часового пояса в запасном тексте даты правится в «Тексты бота»."""
    del owner

    response = await shop.http.put(
        f"{TEXTS}/ru/date.fallback", json={"text": "{date}, {time} (ЕКБ)"}
    )

    assert response.status_code == 200
    texts = await load_texts(shop.session)
    moment = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
    fallback = texts.date_fallback(moment, "ru", ZoneInfo("Asia/Yekaterinburg"))
    assert fallback == "1 ноября 2026, 23:00 (ЕКБ)"


async def test_9_16_default_subscription_name_from_bot_texts(shop: Shop, owner: TeamMember) -> None:
    """1.29, 9.16: название первой подписки — текст `subscription.default_name`, правится
    в «Тексты бота»."""
    del owner
    client = await make_client(shop.session)

    response = await shop.http.put(
        f"{TEXTS}/ru/subscription.default_name", json={"text": "Домашняя"}
    )

    assert response.status_code == 200
    assert await default_name(shop.session, client.id) == "Домашняя"


async def test_1_29_welcome_is_the_same_text_in_brand(shop: Shop, owner: TeamMember) -> None:
    """1.29, 1.11: приветствие в «Бренд» и `welcome_text` в «Тексты бота» — одно значение."""
    del owner

    await shop.http.put(f"{TEXTS}/ru/welcome_text", json={"text": "Привет от енота!"})
    brand = (await shop.http.get(BRAND)).json()

    assert brand["welcome_text"] == "Привет от енота!"
    assert brand["welcome_text_is_default"] is False
    await shop.http.put(
        BRAND, json={"name": "Енот", "primary_color": "#7C3AED", "welcome_text": "Здравствуйте!"}
    )
    item = _items((await shop.http.get(TEXTS)).json())["welcome_text"]
    assert (item["text"], item["changed"]) == ("Здравствуйте!", True)


async def test_1_29_unknown_key_or_language(shop: Shop, owner: TeamMember) -> None:
    """1.29: нет такого текста или языка — 404, ничего не сохраняется."""
    del owner

    assert (await shop.http.put(f"{TEXTS}/ru/no.such.key", json={"text": "x"})).status_code == 404
    assert (await shop.http.put(f"{TEXTS}/de/btn.renew", json={"text": "x"})).status_code == 404
    assert (await shop.http.get(TEXTS, params={"language": "de"})).status_code == 404


async def test_1_29_only_owner(shop: Shop) -> None:
    """1.29, 01-domain: настройки — только владелец; помощник и гость получают отказ."""
    assistant = make_team_member(telegram_id=501, role=TeamRole.ASSISTANT)
    await add(shop.session, assistant)

    anonymous = await shop.http.get(TEXTS)
    await shop.sign_in(assistant)
    requests = [
        await shop.http.get(TEXTS),
        await shop.http.put(f"{TEXTS}/ru/btn.renew", json={"text": "Продлить сейчас"}),
        await shop.http.post(f"{TEXTS}/ru/btn.renew/preview", json={"text": "Продлить"}),
        await shop.http.delete(f"{TEXTS}/ru/btn.renew"),
    ]

    assert anonymous.status_code == 401
    assert [response.status_code for response in requests] == [403, 403, 403, 403]
    assert await shop.session.get(BotTextOverride, ("btn.renew", "ru")) is None
