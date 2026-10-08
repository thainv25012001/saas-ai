from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field

_MAX_PUSH_BODY_BYTES = 64 * 1024
_HISTORY_ID_RE = re.compile(r"^[0-9]{1,128}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class PubSubAuthError(ValueError):
    """The push caller is not an authenticated Google Pub/Sub identity."""


class PubSubPayloadError(ValueError):
    """The push envelope does not contain a valid Gmail mailbox notification."""


class GmailPushMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=255)
    mailbox_email: str = Field(min_length=3, max_length=320)
    history_id: str = Field(min_length=1, max_length=128)


def verify_pubsub_request(
    authorization: str | None,
    *,
    expected_audience: str | None,
    expected_email: str | None,
) -> dict[str, Any]:
    if not expected_audience or not expected_email:
        raise PubSubAuthError("Pub/Sub authentication is not configured")
    scheme, _, bearer = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not bearer or " " in bearer:
        raise PubSubAuthError("Pub/Sub authentication failed")
    try:
        claims = id_token.verify_oauth2_token(
            bearer,
            GoogleAuthRequest(),
            audience=expected_audience,
        )  # type: ignore[no-untyped-call]  # google-auth exposes no type for this verifier
    except Exception as exc:
        raise PubSubAuthError("Pub/Sub authentication failed") from exc
    if not isinstance(claims, dict):
        raise PubSubAuthError("Pub/Sub authentication failed")
    issuer = claims.get("iss")
    email = claims.get("email")
    audience = claims.get("aud")
    if (
        issuer not in {"accounts.google.com", "https://accounts.google.com"}
        or audience != expected_audience
        or not isinstance(email, str)
        or email.casefold() != expected_email.strip().casefold()
        or claims.get("email_verified") is not True
    ):
        raise PubSubAuthError("Pub/Sub authentication failed")
    return claims


def decode_pubsub_envelope(payload: bytes) -> GmailPushMessage:
    if not payload or len(payload) > _MAX_PUSH_BODY_BYTES:
        raise PubSubPayloadError("Pub/Sub push payload is invalid")
    try:
        envelope = json.loads(payload)
        message = envelope["message"]
        message_id = message["messageId"]
        encoded_data = message["data"]
        if not isinstance(message_id, str) or not isinstance(encoded_data, str):
            raise ValueError
        # Pub/Sub sends standard base64; Gmail's guide calls the payload
        # base64url. Accept either alphabet, still rejecting anything else.
        normalized = encoded_data.replace("-", "+").replace("_", "/")
        decoded = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
        notification = json.loads(decoded)
        mailbox_email = notification["emailAddress"]
        history_id = notification["historyId"]
        if isinstance(history_id, int) and not isinstance(history_id, bool) and history_id >= 0:
            # Documented as a string, seen as a JSON number in live pushes.
            history_id = str(history_id)
        if (
            not isinstance(mailbox_email, str)
            or not _EMAIL_RE.fullmatch(mailbox_email.strip())
            or not isinstance(history_id, str)
            or not _HISTORY_ID_RE.fullmatch(history_id)
        ):
            raise ValueError
        return GmailPushMessage(
            message_id=message_id,
            mailbox_email=mailbox_email.strip().casefold(),
            history_id=history_id,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise PubSubPayloadError("Pub/Sub push payload is invalid") from exc


def gmail_sync_job_id(mailbox_email: str, history_id: str) -> str:
    identity = f"{mailbox_email.strip().casefold()}:{history_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return f"gmail-sync:{digest}"
