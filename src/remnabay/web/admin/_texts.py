"""«Настройки → Тексты бота» (1.29): все тексты по экранам, правка, предпросмотр, сброс.

Язык — параметр запроса: в MVP тексты только на русском и выбора нет (решение 0075),
с английскими текстами в v1 тот же API отдаёт и правит их по языку.

Предпросмотр и сохранение проверяют текст одинаково: правка подставляется в тексты
бота, текст собирается той же подстановкой, что при отправке, с примерами значений
переменных, и длина сверяется с ограничением Telegram. Что не прошло — не сохраняется.
Тексты бот и воркер читают из базы при каждом сообщении, поэтому правка действует
сразу, без перезапуска.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from babel.numbers import format_currency, format_decimal
from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from remnabay.brand import brand_name
from remnabay.domain.tariffs import Tariff
from remnabay.shop import SHOP_CURRENCY, SHOP_SUPPORT_CONTACT
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting, shop_time_zone
from remnabay.texts import (
    DATE_FALLBACK_KEY,
    GROUPS,
    Catalog,
    TextError,
    TextKind,
    Texts,
    allowed_variables,
    check_override,
    group_of,
    kind_of,
    load_default_catalogs,
    load_overrides,
    max_length,
    reset_override,
    save_override,
    substitute,
)
from remnabay.web.admin._brand import REMNABAY_NAME
from remnabay.web.admin._deps import DbSession, Owner

router = APIRouter(prefix="/settings/texts", tags=["settings"])

# Поле ввода: с запасом на переменные, окончательно длину проверяет предпросмотр
MAX_INPUT_LENGTH = 8192

# Примеры значений переменных для предпросмотра: то, чего нет в настройках магазина.
# Видит только команда в админке, клиенту они не уходят
_SAMPLE_TARIFF = "Стандарт"
_SAMPLE_SUPPORT = "@support"
_SAMPLE_PRICE = Decimal(299)
_SAMPLE_DISCOUNT = Decimal("29.90")
_SAMPLE_PAID = Decimal(150)
_SAMPLE_GIGABYTES = (Decimal("12.4"), Decimal(50))
_SAMPLES = {
    "promo_code": "SPRING10",
    "number": "2",
    "used": "1",
    "limit": "3",
    "count": "3",
    "code": "4821",
    "platform": "iOS",
    "model": "iPhone 15",
    "app": "Happ",
    "app_name": "Happ",
    "subscription_link": "https://sub.example.com/a1b2c3",
}


class BotTextOut(BaseModel):
    key: str
    # Текст по умолчанию из 03-bot-texts.md
    default: str
    # Текст, который сейчас отправляет бот
    text: str
    changed: bool
    # Переменные, которые можно использовать: те, что в тексте по умолчанию
    variables: list[str]
    kind: TextKind
    # Форма множественного числа: для каких чисел она используется
    plural_counts: list[int] | None = None


class TextGroupOut(BaseModel):
    id: str
    title: str
    # Раздел, в который вложена группа («События»), если вложена
    section: str | None
    texts: list[BotTextOut]


class BotTextsOut(BaseModel):
    language: str
    # Языки, для которых есть тексты; в MVP — только русский (0075)
    languages: list[str]
    groups: list[TextGroupOut]


class BotTextIn(BaseModel):
    text: Annotated[str, Field(max_length=MAX_INPUT_LENGTH)]


class TextPreviewOut(BaseModel):
    """Текст так, как его получит клиент, или что в нём не так (1.29)."""

    text: str | None
    problem: str | None


type _Language = Annotated[str, Path(max_length=16)]
type _Key = Annotated[str, Path(max_length=100)]


def _visible(catalog: Catalog, key: str) -> bool:
    """Форму множественного числа, которая для целых чисел не используется (у
    русского — `other`), не показываем: править её бессмысленно."""
    return key not in catalog.plural_forms or bool(catalog.plural_counts(key))


def _text_out(
    catalogs: dict[str, Catalog],
    language: str,
    key: str,
    overrides: dict[tuple[str, str], str],
) -> BotTextOut:
    catalog = catalogs[language]
    default = catalog.entries[key]
    override = overrides.get((key, language))
    return BotTextOut(
        key=key,
        default=default,
        text=override if override is not None else default,
        changed=override is not None,
        variables=allowed_variables(catalogs, key),
        kind=kind_of(key),
        plural_counts=catalog.plural_counts(key) if key in catalog.plural_forms else None,
    )


def _catalog(language: str) -> tuple[dict[str, Catalog], Catalog]:
    catalogs = load_default_catalogs()
    if language not in catalogs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Нет текстов на языке {language}")
    return catalogs, catalogs[language]


def _known(catalog: Catalog, key: str) -> None:
    if key not in catalog.entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Нет текста с ключом {key}")


@router.get("")
async def bot_texts(
    session: DbSession,
    owner: Owner,
    language: Annotated[str | None, Query(max_length=16)] = None,
) -> BotTextsOut:
    """Все тексты, сгруппированные по экранам, в порядке 03-bot-texts.md (1.29).
    Язык не указан — язык бота по умолчанию."""
    del owner
    language = language or await get_setting(session, SHOP_LANGUAGE)
    catalogs, catalog = _catalog(language)
    overrides = await load_overrides(session)
    by_group: dict[str, list[BotTextOut]] = {group.id: [] for group in GROUPS}
    for key in catalog.entries:
        if _visible(catalog, key):
            by_group[group_of(key).id].append(_text_out(catalogs, language, key, overrides))
    return BotTextsOut(
        language=language,
        languages=sorted(catalogs),
        groups=[
            TextGroupOut(id=group.id, title=group.title, section=group.section, texts=texts)
            for group in GROUPS
            if (texts := by_group[group.id])
        ],
    )


async def _samples(
    session: DbSession, texts: Texts, language: str, time_zone: ZoneInfo, now: datetime
) -> dict[str, str]:
    """Примеры значений всех переменных: из настроек магазина и текстов бота с
    правкой, остальное — правдоподобные образцы."""
    currency = await get_setting(session, SHOP_CURRENCY)
    tariff = await session.scalar(select(Tariff.name).order_by(Tariff.sort_order, Tariff.id))

    def date(delta: timedelta) -> str:
        return texts.date_fallback(now + delta, language, time_zone)

    def money(amount: Decimal) -> str:
        return format_currency(amount, currency, locale=language)

    def days(count: int) -> str:
        return texts.quantity("unit.days", language, count)

    samples = dict(_SAMPLES)
    samples |= {
        "brand_name": await brand_name(session) or REMNABAY_NAME,
        "support_contact": (await get_setting(session, SHOP_SUPPORT_CONTACT)).strip()
        or _SAMPLE_SUPPORT,
        "welcome_text": texts.render("welcome_text", language),
        "subscription_name": texts.render("subscription.default_name", language),
        "tariff_name": tariff or _SAMPLE_TARIFF,
        "end_date": date(timedelta(days=30)),
        "new_end_date": date(timedelta(days=60)),
        "invoice_expires_at": date(timedelta(minutes=30)),
        "connected_at": date(timedelta(days=-3)),
        "payment_date": date(timedelta(days=-5)),
        "since": date(timedelta(hours=-2)),
        "until": date(timedelta()),
        "days_text": days(30),
        "period": days(30),
        "trial_period": days(3),
        "minutes_text": texts.quantity("unit.minutes", language, 15),
        "price": money(_SAMPLE_PRICE),
        "amount": money(_SAMPLE_PRICE),
        "discount": money(_SAMPLE_DISCOUNT),
        "total": money(_SAMPLE_PRICE - _SAMPLE_DISCOUNT),
        "paid_amount": money(_SAMPLE_PAID),
        "value": format_decimal(_SAMPLE_GIGABYTES[0], format="#,##0.#", locale=language),
    }
    samples["subscriptions_summary"] = texts.render("summary.active", language, **samples)
    return samples


def _key_samples(texts: Texts, key: str, language: str, samples: dict[str, str]) -> dict[str, str]:
    """Переменные с одинаковым именем, но другим смыслом: трафик карточки — в ГБ."""
    if key == "card.traffic.value":
        used, limit = (
            texts.render(
                "unit.gigabytes",
                language,
                value=format_decimal(size, format="#,##0.#", locale=language),
            )
            for size in _SAMPLE_GIGABYTES
        )
        return samples | {"used": used, "limit": limit}
    return samples


async def _render(session: DbSession, language: str, key: str, text: str) -> str:
    """Текст с правкой так, как его соберёт бот, — или `TextError`, если с ним что-то не
    так: переменные, скобки, пустой текст или длина больше, чем примет Telegram."""
    catalogs, catalog = _catalog(language)
    _known(catalog, key)
    check_override(catalogs, key, text)
    overrides = await load_overrides(session)
    overrides[(key, language)] = text.strip()
    texts = Texts(catalogs, overrides, default_language=language)
    time_zone = await shop_time_zone(session)
    now = datetime.now(UTC)
    if key == DATE_FALLBACK_KEY:
        rendered = texts.date_fallback(now + timedelta(days=30), language, time_zone)
    elif key in catalog.plural_forms:
        unit_key, _ = catalog.plural_forms[key]
        counts = catalog.plural_counts(key)
        rendered = (
            texts.quantity(unit_key, language, counts[0])
            if counts
            else substitute(text.strip(), {"count": "1,5"})
        )
    else:
        samples = await _samples(session, texts, language, time_zone, now)
        rendered = texts.render(key, language, **_key_samples(texts, key, language, samples))
    limit = max_length(key)
    length = len(rendered.encode("utf-16-le")) // 2
    if length > limit:
        raise TextError(
            f"С примерами значений переменных текст — {length} знаков, "
            f"а Telegram принимает не больше {limit}"
        )
    return rendered


@router.post("/{language}/{key}/preview")
async def preview_bot_text(
    language: _Language, key: _Key, body: BotTextIn, session: DbSession, owner: Owner
) -> TextPreviewOut:
    """Предпросмотр до сохранения (1.29): ошибка — не отказ, а пояснение рядом с полем."""
    del owner
    try:
        return TextPreviewOut(text=await _render(session, language, key, body.text), problem=None)
    except TextError as error:
        return TextPreviewOut(text=None, problem=str(error))


@router.put("/{language}/{key}")
async def save_bot_text(
    language: _Language, key: _Key, body: BotTextIn, session: DbSession, owner: Owner
) -> BotTextOut:
    """Сохранение: только текст, который прошёл ту же проверку, что предпросмотр (1.29).
    Изменение — в журнал (4.25)."""
    try:
        await _render(session, language, key, body.text)
        await save_override(session, key, language, body.text, member_id=owner.id)
    except TextError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return _text_out(load_default_catalogs(), language, key, await load_overrides(session))


@router.delete("/{language}/{key}")
async def reset_bot_text(
    language: _Language, key: _Key, session: DbSession, owner: Owner
) -> BotTextOut:
    """Сброс к тексту по умолчанию (1.29); в журнал (4.25)."""
    catalogs, catalog = _catalog(language)
    _known(catalog, key)
    await reset_override(session, key, language, member_id=owner.id)
    await session.commit()
    return _text_out(catalogs, language, key, await load_overrides(session))
