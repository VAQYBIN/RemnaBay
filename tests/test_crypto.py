"""Шифрование секретов, введённых в админке (04-operator-settings, решение 0046)."""

import pytest

from remnabay.crypto import SecretBox, SecretDecryptionError, generate_key, is_valid_key

# Не настоящий ключ провайдера — строка для проверки шифрования
PLAINTEXT = "test-provider-key-123"


def test_secret_is_stored_encrypted_and_decrypts_back() -> None:
    """Секрет хранится только зашифрованным; ключом из .env он расшифровывается обратно."""
    box = SecretBox(generate_key())

    token = box.encrypt(PLAINTEXT)

    assert PLAINTEXT not in token
    assert box.decrypt(token) == PLAINTEXT


def test_same_secret_encrypts_differently_each_time() -> None:
    """По шифротексту нельзя понять, что у двух провайдеров один и тот же ключ."""
    box = SecretBox(generate_key())

    assert box.encrypt(PLAINTEXT) != box.encrypt(PLAINTEXT)


def test_other_key_or_tampered_token_does_not_decrypt() -> None:
    """Сменённый ключ или испорченные данные — понятная ошибка, а не мусор вместо секрета."""
    token = SecretBox(generate_key()).encrypt(PLAINTEXT)
    box = SecretBox(generate_key())

    with pytest.raises(SecretDecryptionError):
        box.decrypt(token)
    with pytest.raises(SecretDecryptionError):
        SecretBox(generate_key()).decrypt(token[:-4] + "AAAA")


def test_key_format_is_checked() -> None:
    """Ключ из generate_key подходит; пароль или пустая строка — нет."""
    assert is_valid_key(generate_key())
    assert not is_valid_key("my-secret-password")
    assert not is_valid_key("")
