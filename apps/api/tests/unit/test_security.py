import time
from datetime import timedelta

import pytest

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
