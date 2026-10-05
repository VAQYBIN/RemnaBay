"""Проверка данных, которые Telegram добавляет к адресу кнопки `login_url` (1.4).

Подпись: HMAC-SHA256 от строки «ключ=значение» всех полей, кроме `hash`, по
алфавиту через перевод строки; ключ — SHA-256 токена бота
(core.telegram.org/widgets/login, «Checking authorization»).
"""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

HASH_FIELD = "hash"


@dataclass(frozen=True)
class TelegramAuth:
    telegram_id: int
    auth_date: datetime
    # Подпись: по ней магазин не принимает те же данные второй раз
    signature: str


def verify_login_url(params: Mapping[str, str], bot_token: str) -> TelegramAuth | None:
    """Данные от Telegram с верной подписью, иначе `None`."""
    signature = params.get(HASH_FIELD, "")
    fields = {key: value for key, value in params.items() if key != HASH_FIELD}
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature.lower()):
        return None
    try:
        telegram_id = int(fields["id"])
        auth_date = datetime.fromtimestamp(int(fields["auth_date"]), UTC)
    except KeyError, ValueError, OverflowError:
        return None
    return TelegramAuth(telegram_id=telegram_id, auth_date=auth_date, signature=signature.lower())


def sign_login_url(fields: Mapping[str, str], bot_token: str) -> str:
    """Подпись, как её ставит Telegram, — для тестов и разработки."""
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
