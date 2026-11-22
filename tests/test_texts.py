"""Тексты бота по ключу и языку (решение 0018)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.texts import (
    BotTextOverride,
    TextError,
    Texts,
    check_override,
    load_default_catalogs,
    load_overrides,
    variables_of,
)
from tests.bot_texts_doc import default_texts

MOSCOW = ZoneInfo("Europe/Moscow")
# Единицы с множественным числом — предложение, ещё не утверждённое в спеке
PROPOSED_PLURALS = {"unit.days", "unit.minutes"}


def texts(overrides: dict[tuple[str, str], str] | None = None) -> Texts:
    return Texts(load_default_catalogs(), overrides or {}, default_language="ru")


def test_0018_default_texts_match_bot_texts_spec() -> None:
    """0018: тексты по умолчанию — ровно те, что в 03-bot-texts.md: ключи, тексты и переменные."""
    catalog = load_default_catalogs()["ru"]
    spec = default_texts()

    assert set(catalog.texts) == set(spec)
    for key, text in spec.items():
        assert catalog.texts[key] == text, key
        assert variables_of(catalog.texts[key]) == variables_of(text), key
    assert set(catalog.plurals) == PROPOSED_PLURALS


def test_0018_client_language_or_default() -> None:
    """0018: перевод на язык клиента — если он есть; иначе — язык бота по умолчанию."""
    bot = texts()

    assert bot.language_for("ru") == "ru"
    assert bot.language_for("ru-RU") == "ru"
    assert bot.language_for("en") == "ru"
    assert bot.language_for(None) == "ru"
    assert bot.render("btn.renew", "de") == "Продлить"


def test_0018_operator_override_wins_and_reset_returns_default() -> None:
    """0018: оператор может переписать любой текст; сброс возвращает текст по умолчанию."""
    rewritten = texts({("btn.renew", "ru"): "Продлить сейчас"})

    assert rewritten.render("btn.renew", "ru") == "Продлить сейчас"
    assert texts().render("btn.renew", "ru") == "Продлить"


def test_variables_are_substituted_by_name() -> None:
    """Переменные подставляет магазин; без нужной переменной текст не собирается."""
    bot = texts()

    assert bot.render("shop.closed", "ru", brand_name="Бухта").startswith("Бухта скоро откроется")
    with pytest.raises(TextError, match="brand_name"):
        bot.render("shop.closed", "ru")


def test_operator_may_reorder_and_remove_variables_but_not_invent() -> None:
    """03-bot-texts.md: переменные можно переставлять и убирать, но не придумывать новые."""
    catalogs = load_default_catalogs()

    check_override(catalogs, "event.renewed", "До {end_date} продлена «{subscription_name}»")
    check_override(catalogs, "event.renewed", "Подписка продлена")
    with pytest.raises(TextError, match=r"\{price\}"):
        check_override(catalogs, "event.renewed", "Продлено за {price}")
    with pytest.raises(TextError, match="Нет текста"):
        check_override(catalogs, "no.such.key", "текст")


def test_template_cannot_reach_beyond_variable_values() -> None:
    """Шаблон оператора — только подстановка по имени: `{brand_name.__class__}` не выражение."""
    bot = texts({("shop.closed", "ru"): "{brand_name.__class__} {brand_name}"})

    assert bot.render("shop.closed", "ru", brand_name="Бухта") == "{brand_name.__class__} Бухта"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 день"), (3, "3 дня"), (5, "5 дней"), (11, "11 дней"), (21, "21 день"), (22, "22 дня")],
)
def test_0018_numbers_follow_language_plural_rules(count: int, expected: str) -> None:
    """0018: тексты с числами — по правилам множественного числа языка."""
    assert texts().quantity("unit.days", "ru", count) == expected


def test_minutes_plural_for_rate_limit_text() -> None:
    """8.13: «Попробуйте ещё раз через 15 минут» — {minutes_text} с правильной формой."""
    bot = texts()
    minutes = bot.quantity("unit.minutes", "ru", 15)

    assert bot.render("promo.error.rate_limited", "ru", minutes_text=minutes).endswith(
        "через 15 минут."
    )
    assert bot.quantity("unit.minutes", "ru", 1) == "1 минуту"


def test_0044_date_fallback_in_shop_time_zone() -> None:
    """0044: запасной текст даты — во времени магазина, месяц словом на языке клиента."""
    moment = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)

    assert texts().date_fallback(moment, "ru", MOSCOW) == "1 ноября 2026, 23:00 (МСК)"


async def test_overrides_are_stored_per_key_and_language(db_session: AsyncSession) -> None:
    """Правки оператора хранятся по ключу и языку; удаление правки — сброс к умолчанию."""
    db_session.add(BotTextOverride(key="btn.renew", language="ru", text="Продлить сейчас"))
    await db_session.flush()

    assert texts(await load_overrides(db_session)).render("btn.renew", "ru") == "Продлить сейчас"

    await db_session.execute(delete(BotTextOverride))
    assert texts(await load_overrides(db_session)).render("btn.renew", "ru") == "Продлить"
