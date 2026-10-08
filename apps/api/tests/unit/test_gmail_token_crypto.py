from __future__ import annotations

import pytest

from app.gmail.token_crypto import decrypt_token, encrypt_token


def test_token_encryption_round_trip_uses_versioned_ciphertext() -> None:
    key = b"k" * 32

    ciphertext = encrypt_token("refresh-token-value", key=key)

    assert ciphertext.startswith("v1:")
    assert "refresh-token-value" not in ciphertext
    assert decrypt_token(ciphertext, key=key) == "refresh-token-value"


def test_wrong_key_and_tampered_ciphertext_are_rejected() -> None:
    ciphertext = encrypt_token("secret", key=b"a" * 32)

    with pytest.raises(ValueError, match="could not be decrypted"):
        decrypt_token(ciphertext, key=b"b" * 32)
    with pytest.raises(ValueError, match="could not be decrypted"):
        decrypt_token(ciphertext[:-2] + "AA", key=b"a" * 32)


def test_missing_or_malformed_encryption_key_never_falls_back_to_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("GMAIL_TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="encryption key is not configured"):
        encrypt_token("secret")
