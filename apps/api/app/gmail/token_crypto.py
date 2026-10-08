from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_PREFIX = "v1:"
_AAD = b"gmail-refresh-token:v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12


def encrypt_token(plaintext: str, *, key: bytes | None = None) -> str:
    """Encrypt a Gmail token with AES-256-GCM; missing keys are hard errors."""
    if not plaintext:
        raise ValueError("token must not be empty")
    resolved_key = _resolve_key(key)
    nonce = os.urandom(_NONCE_BYTES)
    encrypted = AESGCM(resolved_key).encrypt(nonce, plaintext.encode("utf-8"), _AAD)
    token = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")
    return f"{_PREFIX}{token}"


def decrypt_token(ciphertext: str, *, key: bytes | None = None) -> str:
    """Decrypt only the supported authenticated format; never return partial data."""
    if not ciphertext.startswith(_PREFIX):
        raise ValueError("stored Gmail token could not be decrypted")
    try:
        payload = base64.urlsafe_b64decode(ciphertext[len(_PREFIX) :] + "===")
        if len(payload) <= _NONCE_BYTES:
            raise ValueError
        nonce, encrypted = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
        plaintext = AESGCM(_resolve_key(key)).decrypt(nonce, encrypted, _AAD)
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("stored Gmail token could not be decrypted") from exc


def _resolve_key(key: bytes | None) -> bytes:
    if key is not None:
        if len(key) != _KEY_BYTES:
            raise ValueError("Gmail token encryption key must be 32 bytes")
        return key
    encoded_key = get_settings().gmail_token_encryption_key
    if not encoded_key:
        raise RuntimeError("Gmail token encryption key is not configured")
    try:
        resolved = base64.b64decode(encoded_key, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Gmail token encryption key must be base64 encoded") from exc
    if len(resolved) != _KEY_BYTES:
        raise RuntimeError("Gmail token encryption key must decode to exactly 32 bytes")
    return resolved
