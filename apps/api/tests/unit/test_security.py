import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.ids import uuid7
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_is_not_the_plaintext():
    assert hash_password("hunter2") != "hunter2"


def test_hash_uses_argon2id():
    assert hash_password("hunter2").startswith("$argon2id$")


def test_hashes_are_salted_and_therefore_differ():
    assert hash_password("hunter2") != hash_password("hunter2")


def test_correct_password_verifies():
    assert verify_password("hunter2", hash_password("hunter2")) is True


def test_wrong_password_does_not_verify():
    assert verify_password("wrong", hash_password("hunter2")) is False


def test_malformed_hash_does_not_verify_and_does_not_raise():
    assert verify_password("hunter2", "not-a-hash") is False


def test_access_token_round_trips_its_claims():
    user_id, org_id = uuid7(), uuid7()
    token = create_access_token(user_id=user_id, organization_id=org_id, role="owner")
    payload = decode_token(token, expected_type="access")
    assert payload.sub == user_id
    assert payload.org == org_id
    assert payload.role == "owner"
    assert payload.typ == "access"


def test_refresh_token_returns_a_jti_matching_its_payload():
    token, jti = create_refresh_token(user_id=uuid7())
    payload = decode_token(token, expected_type="refresh")
    assert payload.jti == jti


def test_a_refresh_token_is_rejected_where_an_access_token_is_required():
    """Token confusion is a real attack: a long-lived refresh token must not
    be usable as a short-lived access token."""
    token, _ = create_refresh_token(user_id=uuid7())
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")


def test_tampered_token_is_rejected():
    token = create_access_token(user_id=uuid7(), organization_id=uuid7(), role="owner")
    with pytest.raises(AuthenticationError):
        decode_token(token + "x", expected_type="access")


def test_garbage_is_rejected():
    with pytest.raises(AuthenticationError):
        decode_token("not.a.token", expected_type="access")


def test_expired_token_is_rejected(monkeypatch):
    from app.core import security

    monkeypatch.setattr(security, "_access_token_lifetime", lambda: timedelta(seconds=-1))
    token = security.create_access_token(user_id=uuid7(), organization_id=uuid7(), role="owner")
    time.sleep(0.01)
    with pytest.raises(AuthenticationError):
        security.decode_token(token, expected_type="access")


def test_token_missing_required_claim_raises_authentication_error() -> None:
    """Regression test: validly-signed tokens missing required claims must raise
    AuthenticationError (not pydantic.ValidationError), maintaining the contract
    that decode_token never escapes validation errors to the caller."""
    # Create a token with a valid signature but missing the required 'jti' claim.
    # The token is signed with the correct secret, so jwt.decode will succeed.
    # But TokenPayload.model_validate will fail because jti is missing.
    # We must catch that ValidationError and convert it to AuthenticationError.
    claims = {
        "sub": str(uuid7()),
        "typ": "access",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
        # Note: 'jti' is deliberately omitted.
    }
    token = jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")


def test_token_with_invalid_signature_raises_authentication_error() -> None:
    """Verify that signature verification is enforced: a token signed with
    a different secret must be rejected, even if it has all required claims."""
    user_id = uuid7()
    claims = {
        "sub": str(user_id),
        "org": str(uuid7()),
        "role": "owner",
        "jti": str(uuid7()),
        "typ": "access",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
    }
    # Sign with a different secret (not the one in Settings).
    token = jwt.encode(claims, "some-other-secret-that-is-not-the-real-one", algorithm="HS256")
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")
