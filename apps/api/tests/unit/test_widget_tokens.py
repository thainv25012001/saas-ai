"""Visitor tokens for the public widget (spec §4): HS256 with the existing
`JWT_SECRET`, `typ: "widget"`, 30-day lifetime. The load-bearing property is
that `typ` is enforced in both directions -- a widget token is never an
access token, and an access token is never a widget token."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.ids import uuid7
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.widget import tokens
from app.widget.tokens import (
    WIDGET_TOKEN_TYPE,
    VisitorClaims,
    create_visitor_token,
    decode_visitor_token,
)


def _mint() -> tuple[str, datetime, uuid.UUID, uuid.UUID, str]:
    org_id, agent_id, vid = uuid7(), uuid7(), str(uuid.uuid4())
    token, expires_at = create_visitor_token(
        organization_id=org_id, agent_id=agent_id, visitor_id=vid
    )
    return token, expires_at, org_id, agent_id, vid


def test_round_trip_returns_the_same_claims():
    token, expires_at, org_id, agent_id, vid = _mint()

    claims = decode_visitor_token(token)

    assert claims == VisitorClaims(
        organization_id=org_id, agent_id=agent_id, visitor_id=vid, expires_at=expires_at
    )


def test_token_carries_typ_widget_and_a_30_day_lifetime():
    token, expires_at, *_ = _mint()

    raw = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])

    assert WIDGET_TOKEN_TYPE == "widget"
    assert raw["typ"] == "widget"
    assert set(raw) == {"typ", "org", "agent", "vid", "iat", "exp"}
    assert expires_at.tzinfo is not None
    assert int(expires_at.timestamp()) == raw["exp"]
    lifetime = expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < lifetime <= timedelta(days=30)


def test_an_access_token_is_not_a_widget_token():
    access = create_access_token(user_id=uuid7(), organization_id=uuid7(), role="owner")
    with pytest.raises(AuthenticationError):
        decode_visitor_token(access)


def test_a_refresh_token_is_not_a_widget_token():
    refresh, _jti = create_refresh_token(user_id=uuid7())
    with pytest.raises(AuthenticationError):
        decode_visitor_token(refresh)


def test_a_widget_token_is_not_an_access_token():
    token, *_ = _mint()
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")


def test_a_widget_token_is_not_a_refresh_token():
    token, *_ = _mint()
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="refresh")


def test_a_token_with_typ_widget_signed_elsewhere_is_refused():
    token, *_ = _mint()
    raw = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    forged = jwt.encode(raw, "an-entirely-different-secret-of-sufficient-length", algorithm="HS256")
    with pytest.raises(AuthenticationError):
        decode_visitor_token(forged)


def test_a_tampered_signature_is_refused():
    token, *_ = _mint()
    header, payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(AuthenticationError):
        decode_visitor_token(f"{header}.{payload}.{flipped}")


def test_a_tampered_payload_is_refused():
    token, *_ = _mint()
    header, _payload, signature = token.split(".")
    other, *_ = _mint()
    _h, other_payload, _s = other.split(".")
    with pytest.raises(AuthenticationError):
        decode_visitor_token(f"{header}.{other_payload}.{signature}")


def test_an_expired_token_is_refused(monkeypatch):
    monkeypatch.setattr(tokens, "_token_lifetime", lambda: timedelta(seconds=-60))
    token, *_ = _mint()
    with pytest.raises(AuthenticationError):
        decode_visitor_token(token)


def test_a_widget_typed_token_missing_claims_is_refused():
    secret = get_settings().jwt_secret
    exp = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
    for claims in (
        {"typ": "widget", "agent": str(uuid7()), "vid": "v", "exp": exp},
        {"typ": "widget", "org": str(uuid7()), "vid": "v", "exp": exp},
        {"typ": "widget", "org": str(uuid7()), "agent": str(uuid7()), "exp": exp},
        {"typ": "widget", "org": "not-a-uuid", "agent": str(uuid7()), "vid": "v", "exp": exp},
        {"typ": "widget", "org": str(uuid7()), "agent": str(uuid7()), "vid": "", "exp": exp},
    ):
        with pytest.raises(AuthenticationError):
            decode_visitor_token(jwt.encode(claims, secret, algorithm="HS256"))


@pytest.mark.parametrize("garbage", ["", "garbage", "a.b.c", "Bearer x"])
def test_garbage_is_refused(garbage):
    with pytest.raises(AuthenticationError):
        decode_visitor_token(garbage)
