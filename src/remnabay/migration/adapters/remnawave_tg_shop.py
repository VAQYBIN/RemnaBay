"""Адаптер remnawave-tg-shop: база старого бота → промежуточный формат (9.9).

Работает с оригиналом (kavore/remnawave-tg-shop v2.7.0) и форком автора (v4.0.2):
что есть в базе, адаптер узнаёт по столбцам и таблицам, а не по номеру версии.
Базу читает только на чтение. Правила — research/spike-bots-subscriptions.md,
раздел «Этап 10», и решение 0071:
- клиент — строка `users` с Telegram ID больше нуля; веб-клиенты форка (отрицательный
  ID) в файл не попадают;
- «Платил в старом боте» — платёж `succeeded` с суммой больше нуля у любого провайдера;
  в форке — и платежи привязанного веб-аккаунта, и подтверждение активации тарифа;
- «Триал использован» — если старый бот сейчас отказал бы в триале: есть строка
  подписки или активация триала, а разрешения на повторный триал нет;
- реферальные коды — значения параметра `start`, которые принимает старый бот.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from remnabay.migration._format import FileClient, MigrationFile, PanelUserRef, Source

SOURCE_NAME = "remnawave-tg-shop"
# Как бот создаёт пользователя панели (оба варианта)
PANEL_USERNAME = "tg_{telegram_id}"
# Подтверждение активации оплаченного тарифа в форке (без пробного)
_PAID_PURPOSES = ("purchase", "renewal")

type Row = dict[str, Any]


def conninfo_with_read_only(url: str) -> str:
    """Строка подключения, в которой сервер сам запрещает запись в сессии. Параметры
    `options` из адреса сохраняются."""
    options = conninfo_to_dict(url).get("options")
    read_only = "-c default_transaction_read_only=on"
    return make_conninfo(url, options=f"{options} {read_only}" if options else read_only)


def connect(url: str) -> psycopg.Connection[Row]:
    """Подключение только на чтение: и сессия, и каждая транзакция (9.9)."""
    connection = psycopg.Connection[Row].connect(conninfo_with_read_only(url), row_factory=dict_row)
    connection.read_only = True
    return connection


@dataclass(frozen=True)
class Schema:
    """Что есть в базе старого бота."""

    columns: dict[str, set[str]]

    def has(self, table: str, column: str | None = None) -> bool:
        if column is None:
            return table in self.columns
        return column in self.columns.get(table, set())

    @property
    def is_fork(self) -> bool:
        return self.has("users", "panel_user_id")


def _schema(connection: psycopg.Connection[Row]) -> Schema:
    columns: dict[str, set[str]] = defaultdict(set)
    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema()"
    )
    for row in rows:
        columns[row["table_name"]].add(row["column_name"])
    if not {"users", "payments", "subscriptions"} <= columns.keys():
        raise ValueError("Это не база remnawave-tg-shop: нет таблиц users, payments, subscriptions")
    return Schema(dict(columns))


@dataclass(frozen=True)
class _Identity:
    """Клиент старого бота и его веб-аккаунт в форке."""

    telegram_id: int
    site_user_id: int | None = None
    account_id: Any = None

    @property
    def user_ids(self) -> set[int]:
        return {self.telegram_id} | ({self.site_user_id} if self.site_user_id else set())


def _identities(connection: psycopg.Connection[Row], schema: Schema) -> dict[int, _Identity]:
    if not schema.has("accounts", "telegram_user_id"):
        return {}
    site = sql.SQL("site_user_id" if schema.has("accounts", "site_user_id") else "NULL::bigint")
    rows = connection.execute(
        sql.SQL(
            "SELECT telegram_user_id, {} AS site_user_id, id FROM accounts"
            " WHERE telegram_user_id > 0"
        ).format(site)
    )
    return {
        row["telegram_user_id"]: _Identity(
            telegram_id=row["telegram_user_id"],
            site_user_id=row["site_user_id"],
            account_id=row["id"],
        )
        for row in rows
    }


def _paid_user_ids(connection: psycopg.Connection[Row], schema: Schema) -> set[int]:
    """Строки `users`, у которых есть оплата (решение 0071)."""
    evidence = [sql.SQL("p.status = 'succeeded'")]
    if schema.has("payments", "activation_status"):
        evidence.append(sql.SQL("p.activation_status = 'succeeded'"))
    if schema.has("entitlement_payments", "payment_id"):
        evidence.append(
            sql.SQL(
                "EXISTS (SELECT 1 FROM entitlement_payments ep"
                " WHERE ep.payment_id = p.payment_id AND ep.purpose = ANY(%(purposes)s))"
            )
        )
    rows = connection.execute(
        sql.SQL("SELECT DISTINCT p.user_id FROM payments p WHERE p.amount > 0 AND ({})").format(
            sql.SQL(" OR ").join(evidence)
        ),
        {"purposes": list(_PAID_PURPOSES)},
    )
    return {row["user_id"] for row in rows}


@dataclass(frozen=True)
class _TrialRow:
    account_id: Any
    user_id: int | None
    telegram_user_id: int | None
    site_user_id: int | None

    def matches(self, identity: _Identity) -> bool:
        return (
            (self.user_id is not None and self.user_id in identity.user_ids)
            or self.telegram_user_id == identity.telegram_id
            or (identity.site_user_id is not None and self.site_user_id == identity.site_user_id)
            or (identity.account_id is not None and self.account_id == identity.account_id)
        )


def _trial_rows(
    connection: psycopg.Connection[Row], schema: Schema, table: str, open_column: str
) -> list[_TrialRow]:
    """Действующие активации триала или неиспользованные разрешения на повторный (форк)."""
    if not schema.has(table, open_column):
        return []
    rows = connection.execute(
        sql.SQL(
            "SELECT account_id, user_id, telegram_user_id, site_user_id FROM {} WHERE {} IS NULL"
        ).format(sql.Identifier(table), sql.Identifier(open_column))
    )
    return [
        _TrialRow(
            account_id=row["account_id"],
            user_id=row["user_id"],
            telegram_user_id=row["telegram_user_id"],
            site_user_id=row["site_user_id"],
        )
        for row in rows
    ]


def _subscriptions(
    connection: psycopg.Connection[Row], schema: Schema
) -> dict[int, list[int | None]]:
    """Строки подписок клиента: числовые ID панели (форк), новые — первыми."""
    panel = sql.SQL(
        "panel_user_id" if schema.has("subscriptions", "panel_user_id") else "NULL::bigint"
    )
    rows = connection.execute(
        sql.SQL(
            "SELECT user_id, {} AS panel_user_id FROM subscriptions"
            " ORDER BY end_date DESC NULLS LAST"
        ).format(panel)
    )
    by_user: dict[int, list[int | None]] = defaultdict(list)
    for row in rows:
        by_user[row["user_id"]].append(row["panel_user_id"])
    return by_user


def _codes(telegram_id: int, code: str | None) -> list[str]:
    """Параметры `start`, которые старый бот принимает как ссылку на этого клиента."""
    codes: list[str] = []
    if code and code.strip():
        code = code.strip()
        codes += [f"ref_u{code}", f"ref_U{code}", f"ref_{code}"]
    codes.append(f"ref_{telegram_id}")
    return codes


def _panel_users(
    telegram_id: int, panel_user_id: int | None, rows: list[int | None]
) -> list[PanelUserRef]:
    """Пользователь панели клиента. Числовой ID — из `users` или самой свежей строки
    подписки (форк); иначе — имя, по которому бот создавал пользователя. Без строк
    подписок пользователя панели у клиента не было."""
    known = panel_user_id or next((row for row in rows if row is not None), None)
    if known is not None:
        return [PanelUserRef(id=known)]
    if rows:
        return [PanelUserRef(username=PANEL_USERNAME.format(telegram_id=telegram_id))]
    return []


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def export(connection: psycopg.Connection[Row]) -> MigrationFile:
    """Клиенты старого бота в промежуточном формате."""
    schema = _schema(connection)
    identities = _identities(connection, schema)
    paid = _paid_user_ids(connection, schema)
    subscriptions = _subscriptions(connection, schema)
    activations = _trial_rows(connection, schema, "trial_activations", "reset_at")
    grants = _trial_rows(connection, schema, "trial_reset_grants", "used_at")
    panel_column = sql.SQL("panel_user_id" if schema.is_fork else "NULL::bigint")
    users = connection.execute(
        sql.SQL(
            "SELECT user_id, username, first_name, last_name, language_code, registration_date,"
            " referral_code, referred_by_id, {} AS panel_user_id"
            " FROM users WHERE user_id > 0 ORDER BY user_id"
        ).format(panel_column)
    )
    clients: list[FileClient] = []
    for user in users:
        telegram_id: int = user["user_id"]
        identity = identities.get(telegram_id, _Identity(telegram_id=telegram_id))
        rows = [row for user_id in identity.user_ids for row in subscriptions.get(user_id, [])]
        activated = any(row.matches(identity) for row in activations)
        granted = any(row.matches(identity) for row in grants)
        name = " ".join(part for part in (user["first_name"], user["last_name"]) if part)
        inviter = user["referred_by_id"]
        clients.append(
            FileClient(
                telegram_id=telegram_id,
                username=user["username"] or None,
                name=name or None,
                language=user["language_code"] or None,
                first_seen_at=_aware(user["registration_date"]),
                trial_used=activated or (bool(rows) and not granted),
                paid_in_old_bot=bool(identity.user_ids & paid),
                panel_users=_panel_users(telegram_id, user["panel_user_id"], rows),
                referral_codes=_codes(telegram_id, user["referral_code"]),
                invited_by=inviter if inviter and inviter > 0 and inviter != telegram_id else None,
            )
        )
    version = "схема форка (v4)" if schema.is_fork else "схема оригинала (v2.7)"
    return MigrationFile(
        format_version=1, source=Source(name=SOURCE_NAME, version=version), clients=clients
    )


def summary(file: MigrationFile) -> dict[str, int]:
    """Итог без Telegram ID и имён — для вывода в консоль."""
    clients = file.clients
    return {
        "клиентов": len(clients),
        "платили": sum(client.paid_in_old_bot is True for client in clients),
        "не платили": sum(client.paid_in_old_bot is False for client in clients),
        "использовали триал": sum(client.trial_used for client in clients),
        "со связью с панелью": sum(bool(client.panel_users) for client in clients),
        "с пригласившим": sum(client.invited_by is not None for client in clients),
    }


def run(url: str, output: str) -> dict[str, int]:
    """Читает базу по адресу и пишет файл. Транзакция только на чтение откатывается."""
    with connect(url) as connection:
        try:
            file = export(connection)
        finally:
            connection.rollback()
    Path(output).write_text(file.model_dump_json(exclude_none=True, indent=2), encoding="utf-8")
    return summary(file)
