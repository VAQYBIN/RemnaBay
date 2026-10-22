"""Прокси для запросов к Telegram (1.28, решение 0074).

Вместо прокси — маленький сервер, который принимает соединение, записывает, куда
клиент просит его соединить (CONNECT у HTTP-прокси, адрес в запросе SOCKS5), и
закрывает соединение. Сам запрос к Telegram при этом не проходит — проверяется
только то, что клиент пошёл через прокси и куда.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import AiogramError

from remnabay.access import OidcAttempt, OidcError, TelegramOidc
from remnabay.bot import create_bot
from remnabay.messaging import NotAcceptedError, OutgoingMessage, TelegramSender
from tests.conftest import REQUIRED_ENV

TOKEN = REQUIRED_ENV["BOT_TOKEN"]


async def _http_connect(reader: asyncio.StreamReader, targets: list[str]) -> None:
    request_line = (await reader.readline()).decode()
    # CONNECT api.telegram.org:443 HTTP/1.1
    method, target, _ = request_line.split(" ", 2)
    if method == "CONNECT":
        targets.append(target)


async def _socks5_connect(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, targets: list[str]
) -> None:
    # Приветствие: версия, число способов входа, способы; отвечаем «без пароля»
    # или «логин и пароль», если клиент их предложил
    _version, count = await reader.readexactly(2)
    methods = await reader.readexactly(count)
    if 2 in methods:
        writer.write(b"\x05\x02")
        await writer.drain()
        _ver, user_length = await reader.readexactly(2)
        await reader.readexactly(user_length)
        (password_length,) = await reader.readexactly(1)
        await reader.readexactly(password_length)
        writer.write(b"\x01\x00")
    else:
        writer.write(b"\x05\x00")
    await writer.drain()
    # Запрос: версия, команда CONNECT, резерв, тип адреса, адрес, порт
    _ver, _command, _reserved, address_type = await reader.readexactly(4)
    if address_type == 3:
        (length,) = await reader.readexactly(1)
        host = (await reader.readexactly(length)).decode()
    else:
        host = ".".join(str(byte) for byte in await reader.readexactly(4))
    port = int.from_bytes(await reader.readexactly(2))
    targets.append(f"{host}:{port}")


@asynccontextmanager
async def _fake_proxy(scheme: str) -> AsyncGenerator[tuple[str, list[str]]]:
    targets: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(asyncio.IncompleteReadError, ConnectionError, ValueError):
            if scheme == "http":
                await _http_connect(reader, targets)
            else:
                await _socks5_connect(reader, writer, targets)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    credentials = "user:secret@" if scheme == "socks5" else ""
    async with server:
        yield f"{scheme}://{credentials}127.0.0.1:{port}", targets


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_1_28_web_bot_goes_through_proxy(scheme: str) -> None:
    """1.28: бот веба (вебхук, ответы клиентам, «Применить в Telegram») ходит в
    Telegram через прокси."""
    async with _fake_proxy(scheme) as (proxy, targets):
        bot = create_bot(TOKEN, proxy=proxy)
        try:
            with pytest.raises(AiogramError):
                await bot.get_me()
        finally:
            await bot.session.close()

    assert targets == ["api.telegram.org:443"]


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_1_28_worker_sender_goes_through_proxy(scheme: str) -> None:
    """1.28, 4.33: отправитель воркера (уведомления, напоминания, рассылки) ходит в
    Telegram через прокси; сбой прокси — «Telegram сообщение не принял», его можно
    отправить снова."""
    async with _fake_proxy(scheme) as (proxy, targets):
        sender = TelegramSender(TOKEN, proxy=proxy)
        try:
            with pytest.raises(NotAcceptedError):
                await sender.send(100, OutgoingMessage(text="Проверка"))
        finally:
            await sender.close()

    assert targets == ["api.telegram.org:443"]


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_1_28_admin_login_goes_through_proxy(scheme: str) -> None:
    """1.28: вход в админку через Telegram (обмен кода на oauth.telegram.org) идёт
    через прокси."""
    async with _fake_proxy(scheme) as (proxy, targets):
        oidc = TelegramOidc("123456", "secret", proxy=proxy)
        try:
            with pytest.raises(OidcError):
                await oidc.telegram_id(
                    "code", OidcAttempt.new(), "https://shop.example.com/cb", now=datetime.now(UTC)
                )
        finally:
            await oidc.aclose()

    assert targets == ["oauth.telegram.org:443"]
