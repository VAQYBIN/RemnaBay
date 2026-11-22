"""Шифрование секретов, введённых в админке (04-operator-settings, решение 0046).

Ключи платёжных провайдеров и другие секреты из админки хранятся в базе только в
зашифрованном виде; ключ шифрования лежит в `.env` (ENCRYPTION_KEY). Рецепт —
Fernet: AES с проверкой целостности, подделанный или чужой шифротекст не
расшифруется.
"""

from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptionError(Exception):
    """Секрет не расшифровывается: ключ шифрования сменили или потеряли, либо данные испорчены."""


def generate_key() -> str:
    """Новый ключ шифрования для `.env`."""
    return Fernet.generate_key().decode()


def is_valid_key(key: str) -> bool:
    try:
        Fernet(key)
    except ValueError:
        return False
    return True


class SecretBox:
    """Шифрует и расшифровывает секреты ключом из `.env`."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, token: str, *, max_age: timedelta | None = None) -> str:
        """`max_age` — шифротекст старше этого тоже не расшифровывается (срок жизни)."""
        ttl = int(max_age.total_seconds()) if max_age is not None else None
        try:
            return self._fernet.decrypt(token, ttl=ttl).decode()
        except InvalidToken as error:
            raise SecretDecryptionError(
                "Секрет не расшифровывается ключом из ENCRYPTION_KEY"
            ) from error
