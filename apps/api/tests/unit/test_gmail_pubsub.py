from __future__ import annotations

import base64
import json

import pytest

from app.gmail.pubsub import (
    PubSubAuthError,
    PubSubPayloadError,
    decode_pubsub_envelope,
    gmail_sync_job_id,
    verify_pubsub_request,
)


def _envelope(data: bytes) -> bytes:
    return json.dumps(
        {
            "message": {
                "messageId": "message-1",
                "data": base64.b64encode(data).decode("ascii"),
            }
        }
    ).encode()


def test_valid_pubsub_token_requires_google_issuer_email_audience_and_verified_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gmail import pubsub

    seen: dict[str, object] = {}

    def verify(token: str, _request: object, *, audience: str) -> dict[str, object]:
        seen.update(token=token, audience=audience)
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": "push@example.iam.gserviceaccount.com",
            "email_verified": True,
        }

    monkeypatch.setattr(pubsub.id_token, "verify_oauth2_token", verify)
    claims = verify_pubsub_request(
        "Bearer signed.jwt.value",
        expected_audience="https://api.example.com/pubsub",
        expected_email="push@example.iam.gserviceaccount.com",
    )

    assert claims["email_verified"] is True
    assert seen == {
        "token": "signed.jwt.value",
        "audience": "https://api.example.com/pubsub",
    }


@pytest.mark.parametrize(
    "claims",
    [
        {
            "iss": "attacker",
            "aud": "https://api.example.com/p",
            "email": "p@example.com",
            "email_verified": True,
        },
        {
            "iss": "accounts.google.com",
            "aud": "wrong",
            "email": "p@example.com",
            "email_verified": True,
        },
        {
            "iss": "accounts.google.com",
            "aud": "https://api.example.com/p",
            "email": "attacker@example.com",
            "email_verified": True,
        },
        {
            "iss": "accounts.google.com",
            "aud": "https://api.example.com/p",
            "email": "p@example.com",
            "email_verified": False,
        },
    ],
)
def test_invalid_pubsub_claims_are_rejected(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, object]
) -> None:
    from app.gmail import pubsub

    monkeypatch.setattr(pubsub.id_token, "verify_oauth2_token", lambda *_a, **_k: claims)
    with pytest.raises(PubSubAuthError):
        verify_pubsub_request(
            "Bearer signed.jwt.value",
            expected_audience="https://api.example.com/p",
            expected_email="p@example.com",
        )


def test_missing_or_invalid_bearer_token_is_rejected_without_echoing_token() -> None:
    with pytest.raises(PubSubAuthError) as missing:
        verify_pubsub_request(None, expected_audience="aud", expected_email="push@example.com")
    assert "None" not in str(missing.value)

    with pytest.raises(PubSubAuthError) as invalid:
        verify_pubsub_request(
            "Bearer private-jwt",
            expected_audience="aud",
            expected_email="push@example.com",
        )
    assert "private-jwt" not in str(invalid.value)


def test_pubsub_envelope_decodes_only_mailbox_and_history_id() -> None:
    payload = _envelope(b'{"emailAddress":"Quotes@example.com","historyId":"42"}')
    message = decode_pubsub_envelope(payload)

    assert message.message_id == "message-1"
    assert message.mailbox_email == "quotes@example.com"
    assert message.history_id == "42"


def test_pubsub_envelope_accepts_a_numeric_history_id() -> None:
    payload = _envelope(b'{"emailAddress":"quotes@example.com","historyId":9876543210}')

    assert decode_pubsub_envelope(payload).history_id == "9876543210"


def test_pubsub_envelope_accepts_url_safe_base64_data() -> None:
    import base64
    import json

    # Chosen so the standard alphabet would need "+" or "/": the URL-safe
    # encoding of this payload contains "-" or "_" instead.
    data = json.dumps({"emailAddress": "quotes@example.com", "historyId": "42", "pad": "~~~>"})
    encoded = base64.urlsafe_b64encode(data.encode()).decode()
    assert "-" in encoded or "_" in encoded
    payload = json.dumps({"message": {"messageId": "message-1", "data": encoded}}).encode()

    assert decode_pubsub_envelope(payload).history_id == "42"


def test_job_identity_is_stable_for_duplicate_and_case_variant_notifications() -> None:
    assert gmail_sync_job_id("Quotes@example.com", "42") == gmail_sync_job_id(
        "quotes@example.com", "42"
    )
    assert gmail_sync_job_id("quotes@example.com", "42") != gmail_sync_job_id(
        "quotes@example.com", "43"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"message":{"messageId":"x","data":"%%%"}}',
        _envelope(b"not-json"),
        _envelope(b'{"emailAddress":"not-an-email","historyId":"42"}'),
        _envelope(b'{"emailAddress":"valid@example.com","historyId":"bad"}'),
    ],
)
def test_malformed_pubsub_envelopes_fail_with_bounded_error(payload: bytes) -> None:
    with pytest.raises(PubSubPayloadError) as error:
        decode_pubsub_envelope(payload)
    assert len(str(error.value)) < 160
    assert "valid@example.com" not in str(error.value)
