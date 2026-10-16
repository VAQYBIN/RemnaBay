"""Адаптер remnawave-tg-shop (9.9, решения 0070, 0071): база старого бота — схема
оригинала v2.7.0 и форка v4.0.2 — в промежуточный формат, только на чтение.

Схемы ниже — свои, по описанию столбцов из research/spike-bots-subscriptions.md:
код старых ботов в проект не переносится.
"""

import json
from collections.abc import Generator
from pathlib import Path
from typing import LiteralString
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql

from remnabay.cli import main
from remnabay.migration import FileClient, parse_file
from remnabay.migration.adapters import remnawave_tg_shop
from tests.conftest import REQUIRED_ENV

ORIGINAL_DDL: LiteralString = """
CREATE TABLE users (
    user_id bigint PRIMARY KEY, username varchar, first_name varchar, last_name varchar,
    language_code varchar, registration_date timestamptz DEFAULT now(),
    referral_code varchar(16), referred_by_id bigint, panel_user_uuid varchar
);
CREATE TABLE payments (
    payment_id serial PRIMARY KEY, user_id bigint NOT NULL, provider varchar NOT NULL,
    amount double precision NOT NULL, currency varchar NOT NULL, status varchar NOT NULL
);
CREATE TABLE subscriptions (
    subscription_id serial PRIMARY KEY, user_id bigint, panel_user_uuid varchar,
    end_date timestamptz NOT NULL
);
"""

FORK_DDL: LiteralString = (
    ORIGINAL_DDL
    + """
ALTER TABLE users ADD COLUMN panel_user_id bigint;
ALTER TABLE subscriptions ADD COLUMN panel_user_id bigint;
ALTER TABLE payments ADD COLUMN activation_status varchar(30);
CREATE TABLE accounts (id uuid PRIMARY KEY, telegram_user_id bigint, site_user_id bigint);
CREATE TABLE trial_activations (
    id serial PRIMARY KEY, account_id uuid, user_id bigint NOT NULL, telegram_user_id bigint,
    site_user_id bigint, reset_at timestamptz
);
CREATE TABLE trial_reset_grants (
    id serial PRIMARY KEY, account_id uuid, user_id bigint NOT NULL, telegram_user_id bigint,
    site_user_id bigint, used_at timestamptz
);
CREATE TABLE entitlement_payments (
    id serial PRIMARY KEY, entitlement_id int, payment_id int, purpose varchar(50) NOT NULL
);
"""
)

FORK_DATA: LiteralString = """
INSERT INTO users (user_id, username, first_name, last_name, language_code, registration_date,
                   referral_code, referred_by_id, panel_user_id) VALUES
 (101, 'ivan', 'Иван', 'Петров', 'ru', '2024-05-01 10:00+00', 'AAAAAAAA1', NULL, 5001),
 (102, NULL, 'Звёзды', NULL, NULL, '2024-05-02 10:00+00', 'AAAAAAAA2', NULL, NULL),
 (103, NULL, 'Активация', NULL, 'ru', now(), 'AAAAAAAA3', NULL, NULL),
 (104, NULL, 'Отменён', NULL, 'ru', now(), 'AAAAAAAA4', NULL, NULL),
 (105, NULL, 'Веб', NULL, 'ru', now(), 'AAAAAAAA5', NULL, NULL),
 (106, NULL, 'Триал', NULL, 'ru', now(), 'AAAAAAAA6', NULL, NULL),
 (107, NULL, 'Разрешение', NULL, 'ru', now(), 'AAAAAAAA7', NULL, NULL),
 (108, NULL, 'Приглашён', NULL, 'pt-br', now(), 'ABCDEF123', 101, NULL),
 (109, NULL, 'Старый', NULL, 'en', now(), 'AAAAAAAA9', -7, NULL),
 (110, NULL, 'Ноль', NULL, 'ru', now(), NULL, 110, NULL),
 (-7, 'web_abc', 'mail@example.com', NULL, 'ru', now(), 'WEBWEBWEB', NULL, NULL);
INSERT INTO accounts (id, telegram_user_id, site_user_id) VALUES
 ('00000000-0000-0000-0000-000000000001', 105, -7);
INSERT INTO payments (payment_id, user_id, provider, amount, currency, status,
                      activation_status) VALUES
 (1, 101, 'yookassa', 199, 'RUB', 'succeeded', NULL),
 (2, 101, 'yookassa', 199, 'RUB', 'canceled', NULL),
 (3, 102, 'telegram_stars', 100, 'XTR', 'succeeded', NULL),
 (4, 103, 'platega', 199, 'RUB', 'canceled', 'succeeded'),
 (5, 104, 'platega', 199, 'RUB', 'canceled', NULL),
 (6, -7, 'yookassa', 199, 'RUB', 'succeeded', NULL),
 (7, 110, 'yookassa', 0, 'RUB', 'succeeded', NULL),
 (8, 106, 'yookassa', 199, 'RUB', 'pending_yookassa', NULL);
INSERT INTO entitlement_payments (entitlement_id, payment_id, purpose) VALUES (1, 5, 'trial');
INSERT INTO subscriptions (user_id, panel_user_id, end_date) VALUES
 (101, 4000, '2024-01-01 00:00+00'),
 (101, 5001, '2026-01-01 00:00+00'),
 (107, 5007, '2026-01-01 00:00+00'),
 (109, NULL, '2026-01-01 00:00+00'),
 (-7, 5005, '2026-01-01 00:00+00');
INSERT INTO trial_activations (user_id, telegram_user_id) VALUES (106, 106);
INSERT INTO trial_activations (user_id, telegram_user_id, reset_at) VALUES (104, 104, now());
INSERT INTO trial_reset_grants (user_id, telegram_user_id) VALUES (107, 107);
"""

ORIGINAL_DATA: LiteralString = """
INSERT INTO users (user_id, username, first_name, language_code, referral_code) VALUES
 (201, 'paid', 'Платил', 'ru', 'BBBBBBBB1'),
 (202, NULL, 'Триал', NULL, 'BBBBBBBB2'),
 (203, NULL, 'Пусто', 'en', NULL);
INSERT INTO payments (user_id, provider, amount, currency, status) VALUES
 (201, 'cryptopay', 5, 'USDT', 'succeeded'),
 (202, 'yookassa', 199, 'RUB', 'failed');
INSERT INTO subscriptions (user_id, panel_user_uuid, end_date) VALUES
 (201, 'uuid-v2', now()), (202, 'uuid-v2b', now());
"""


def _schema_url(schema: str) -> str:
    return f"{REQUIRED_ENV['DATABASE_URL']}?options={quote(f'-c search_path={schema}')}"


@pytest.fixture
def old_bot() -> Generator[dict[str, str]]:
    """Две базы старого бота — схемы оригинала и форка — в отдельных схемах test-db."""
    schemas = {"original": "oldbot_original", "fork": "oldbot_fork"}
    scripts: dict[str, LiteralString] = {
        "original": ORIGINAL_DDL + ORIGINAL_DATA,
        "fork": FORK_DDL + FORK_DATA,
    }
    with psycopg.connect(REQUIRED_ENV["DATABASE_URL"], autocommit=True) as connection:
        for variant, schema in schemas.items():
            name = sql.Identifier(schema)
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(name))
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(name))
            connection.execute(sql.SQL("SET search_path TO {}").format(name))
            connection.execute(scripts[variant])
        try:
            yield {variant: _schema_url(schema) for variant, schema in schemas.items()}
        finally:
            for schema in schemas.values():
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _export(url: str) -> dict[int, FileClient]:
    with remnawave_tg_shop.connect(url) as connection:
        file = remnawave_tg_shop.export(connection)
    return {client.telegram_id: client for client in file.clients}


def test_9_9_fork_paid_in_old_bot(old_bot: dict[str, str]) -> None:
    """9.9, 0070, 0071: «Платил» — успешный платёж с суммой больше нуля у любого
    провайдера (и Звёзды), платёж привязанного веб-аккаунта, подтверждение активации;
    отменённый, ждущий и нулевой — не оплата."""
    clients = _export(old_bot["fork"])

    paid = {telegram_id: client.paid_in_old_bot for telegram_id, client in clients.items()}
    assert paid == {
        101: True,
        102: True,
        103: True,
        104: False,
        105: True,
        106: False,
        107: False,
        108: False,
        109: False,
        110: False,
    }


def test_9_9_fork_trial_panel_users_codes(old_bot: dict[str, str]) -> None:
    """9.9, 0071: веб-клиенты не попадают в файл; триал — как решает старый бот;
    пользователь панели — числовой ID или имя `tg_<ID>`; коды — параметры `start`,
    которые принимает старый бот; пригласивший — только клиент Telegram."""
    clients = _export(old_bot["fork"])

    assert -7 not in clients
    trial = {telegram_id: client.trial_used for telegram_id, client in clients.items()}
    # 105 — подписка у привязанного веб-аккаунта: старый бот тоже отказал бы в триале
    assert [t for t, used in trial.items() if used] == [101, 105, 106, 109]
    assert [ref.id for ref in clients[105].panel_users] == [5005]
    assert [ref.id for ref in clients[101].panel_users] == [5001]
    assert [ref.username for ref in clients[109].panel_users] == ["tg_109"]
    assert clients[106].panel_users == []
    assert clients[108].referral_codes == [
        "ref_uABCDEF123",
        "ref_UABCDEF123",
        "ref_ABCDEF123",
        "ref_108",
    ]
    assert clients[110].referral_codes == ["ref_110"]
    assert (clients[108].invited_by, clients[109].invited_by, clients[110].invited_by) == (
        101,
        None,
        None,
    )
    first = clients[101]
    assert (first.username, first.name, first.language) == ("ivan", "Иван Петров", "ru")
    assert first.first_seen_at is not None
    assert first.first_seen_at.isoformat() == "2024-05-01T10:00:00+00:00"
    assert (clients[108].language, clients[102].language) == ("pt-br", None)


def test_9_9_original_schema(old_bot: dict[str, str]) -> None:
    """9.9: оригинал без `panel_user_id`, веб-аккаунтов и активаций триала: пользователь
    панели — по имени, триал — по строкам подписок, оплата — криптовалютой тоже."""
    clients = _export(old_bot["original"])

    assert {t: c.paid_in_old_bot for t, c in clients.items()} == {201: True, 202: False, 203: False}
    assert {t: c.trial_used for t, c in clients.items()} == {201: True, 202: True, 203: False}
    assert [ref.username for ref in clients[202].panel_users] == ["tg_202"]
    assert clients[203].panel_users == []


def test_9_9_connection_is_read_only(old_bot: dict[str, str]) -> None:
    """9.9: адаптер читает базу старого бота только на чтение."""
    with (
        remnawave_tg_shop.connect(old_bot["fork"]) as connection,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        connection.execute("DELETE FROM payments")


def test_9_9_command_writes_file_in_format(old_bot: dict[str, str], tmp_path: Path) -> None:
    """9.9, 9.8: отдельная команда пишет файл, который импорт принимает; в выводе — только
    итоги, без Telegram ID."""
    output = tmp_path / "clients.json"

    source = old_bot["fork"]

    code = main(["adapter", "remnawave-tg-shop", "--source", source, "--output", str(output)])

    assert code == 0
    file = parse_file(output.read_bytes())
    assert file.source.name == "remnawave-tg-shop"
    assert len(file.clients) == 10
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert "paid_in_old_bot" in raw["clients"][0]


def test_9_9_command_rejects_other_database(tmp_path: Path) -> None:
    """9.9: база не старого бота — понятная ошибка, файл не пишется."""
    output = tmp_path / "clients.json"
    url = _schema_url("pg_catalog")

    code = main(["adapter", "remnawave-tg-shop", "--source", url, "--output", str(output)])

    assert code == 1
    assert not output.exists()
