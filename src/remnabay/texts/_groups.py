"""Тексты бота по экранам — для «Настройки → Тексты бота» (1.29).

Группы, их порядок и подписи — разделы `docs/03-bot-texts.md` (тест сверяет). Ключи
здесь не перечисляются: группа задаётся началами ключей, и текст попадает в группу
с самым длинным совпавшим началом (`btn.other_apps` — в С2, остальные `btn.*` — в
«Кнопки»). Так новый текст с известным началом сам попадает в свою группу, а текст
без группы ловит тест.
"""

from dataclasses import dataclass
from enum import StrEnum

from remnabay.texts._catalog import TextError

# Ограничения Telegram (Bot API): текст сообщения — 1–4096 знаков, описание команды
# в меню бота — 1–256
MAX_MESSAGE_LENGTH = 4096
MAX_COMMAND_LENGTH = 256


class TextKind(StrEnum):
    """Где клиент видит текст — от этого зависят предпросмотр и ограничение длины."""

    MESSAGE = "message"
    BUTTON = "button"
    # Описание команды в меню бота (1.27)
    COMMAND = "command"
    # Страница-переходник в браузере, не Telegram
    PAGE = "page"


@dataclass(frozen=True)
class TextGroup:
    id: str
    title: str
    # Раздел 03-bot-texts.md, в который вложена группа («События»), если вложена
    section: str | None
    prefixes: tuple[str, ...]


_EVENTS = "События"
_TEAM = "Уведомления команде"
_OTHER = "Прочее"

GROUPS: tuple[TextGroup, ...] = (
    TextGroup("dates", "Даты", None, ("date",)),
    TextGroup("units", "Единицы", None, ("unit",)),
    TextGroup("buttons", "Кнопки", None, ("btn",)),
    TextGroup("shop_closed", "З0. Магазин скоро откроется", None, ("shop.closed",)),
    TextGroup("shop_paused", "З0а. Магазин временно закрыт", None, ("shop.paused",)),
    TextGroup(
        "main_menu",
        "З1. Главное меню",
        None,
        ("menu", "welcome_text", "summary", "subscription"),
    ),
    TextGroup("subscriptions", "З2. Список подписок", None, ("subscriptions",)),
    TextGroup("renew_choice", "З2а. Какую подписку продлить?", None, ("renew",)),
    TextGroup("subscription_card", "З3. Карточка подписки", None, ("card",)),
    TextGroup("tariffs", "З4. Выбор тарифа", None, ("tariffs",)),
    TextGroup("confirmation", "З5. Подтверждение", None, ("confirm",)),
    TextGroup("payment", "З6. Оплата", None, ("payment",)),
    TextGroup("devices", "З7. Устройства", None, ("devices",)),
    TextGroup("promo", "З8. Промокод", None, ("promo",)),
    TextGroup("subscription_ready", "С1. Подписка готова", _EVENTS, ("event.subscription_ready",)),
    TextGroup(
        "guide",
        "С2. Инструкция по подключению",
        _EVENTS,
        ("btn.other_apps", "btn.open_in_browser", "guide"),
    ),
    TextGroup("trial_activated", "С3. Триал активирован", _EVENTS, ("event.trial_activated",)),
    TextGroup(
        "payment_processing",
        "С4. Оплата получена, подписка создаётся",
        _EVENTS,
        ("event.payment_processing",),
    ),
    TextGroup("trial_processing", "С18. Триал создаётся", _EVENTS, ("event.trial_processing",)),
    TextGroup("payment_failed", "С5. Оплата не прошла", _EVENTS, ("event.payment_failed",)),
    TextGroup("renewed", "С6. Подписка продлена", _EVENTS, ("event.renewed",)),
    TextGroup("reminders", "С7. Напоминание", _EVENTS, ("reminder",)),
    TextGroup("expired", "С8. Подписка истекла", _EVENTS, ("event.expired",)),
    TextGroup("trial_ended", "С9. Триал закончился", _EVENTS, ("event.trial_ended",)),
    TextGroup("days_added", "С10. Добавлены дни", _EVENTS, ("event.days_added",)),
    TextGroup("refunded", "С11. Возврат выполнен", _EVENTS, ("event.refunded",)),
    TextGroup(
        "payment_resolved", "С12. Платёж разобран командой", _EVENTS, ("event.payment_resolved",)
    ),
    TextGroup("refund_declined", "С14. В возврате отказано", _EVENTS, ("event.refund_declined",)),
    TextGroup("link_revoked", "С15. Ссылка обновлена", _EVENTS, ("event.link_revoked",)),
    TextGroup(
        "trial_traffic_exhausted",
        "С16. Трафик триала закончился",
        _EVENTS,
        ("event.trial_traffic_exhausted",),
    ),
    TextGroup(
        "action_failed", "С17. Не удалось выполнить действие", _EVENTS, ("event.action_failed",)
    ),
    TextGroup("team", _TEAM, None, ("team",)),
    TextGroup("admin_login", "Вход в админку", _TEAM, ("team.login", "team.admin_link")),
    TextGroup("redirect", "Страница-переходник", None, ("redirect",)),
    TextGroup("help", "/help", _OTHER, ("help", "command")),
    TextGroup("fallback", "Непонятное сообщение", _OTHER, ("fallback",)),
)

# Подписи кнопок: в «Кнопках» и среди текстов экранов
_BUTTON_PREFIXES = ("btn", "tariffs.button", "devices.button_delete")
_COMMAND_PREFIXES = ("command",)
_PAGE_PREFIXES = ("redirect",)


def _matches(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(f"{prefix}.")


def group_of(key: str) -> TextGroup:
    """Группа текста: с самым длинным совпавшим началом ключа."""
    best: tuple[int, TextGroup] | None = None
    for group in GROUPS:
        for prefix in group.prefixes:
            if _matches(key, prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), group)
    if best is None:
        raise TextError(f"Текст {key} не отнесён ни к одному экрану")
    return best[1]


def kind_of(key: str) -> TextKind:
    for kind, prefixes in (
        (TextKind.BUTTON, _BUTTON_PREFIXES),
        (TextKind.COMMAND, _COMMAND_PREFIXES),
        (TextKind.PAGE, _PAGE_PREFIXES),
    ):
        if any(_matches(key, prefix) for prefix in prefixes):
            return kind
    return TextKind.MESSAGE


def max_length(key: str) -> int:
    """Сколько знаков (в единицах UTF-16, как считает Telegram) может быть в тексте
    после подстановки переменных."""
    return MAX_COMMAND_LENGTH if kind_of(key) == TextKind.COMMAND else MAX_MESSAGE_LENGTH
