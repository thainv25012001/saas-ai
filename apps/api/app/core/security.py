import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.errors import AuthenticationError

_ALGORITHM = "HS256"
_hasher = PasswordHasher()


class TokenPayload(BaseModel):
    sub: uuid.UUID
    org: uuid.UUID | None = None
    role: str | None = None
    jti: str
    typ: str
    exp: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """False for a wrong password and for a corrupt hash alike. Callers get a
    boolean, never an exception they might forget to catch."""
    try:
        return _hasher.verify(password_hash, password)
    except Exception:
        return False


def _access_token_lifetime() -> timedelta:
    return timedelta(minutes=get_settings().access_token_ttl_minutes)


def _refresh_token_lifetime() -> timedelta:
    return timedelta(days=get_settings().refresh_token_ttl_days)


def encode_with_expiry(claims: dict[str, object], lifetime: timedelta) -> tuple[str, datetime]:
    """Sign `claims` with `exp`/`iat` added, and return the token together with
    the exact `exp` it carries -- so a caller that must tell its client when
    the token expires (the widget's session response) reports the value
    actually signed, not a second `now()` that may land a second later."""
    now = datetime.now(UTC)
    exp = int((now + lifetime).timestamp())
    payload = {**claims, "exp": exp, "iat": int(now.timestamp())}
    token = jwt.encode(payload, get_settings().jwt_secret, algorithm=_ALGORITHM)
    return token, datetime.fromtimestamp(exp, UTC)


def _encode(claims: dict[str, object], lifetime: timedelta) -> str:
    token, _expires_at = encode_with_expiry(claims, lifetime)
    return token


def create_access_token(*, user_id: uuid.UUID, organization_id: uuid.UUID, role: str) -> str:
    return _encode(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "role": role,
            "jti": str(uuid.uuid4()),
            "typ": "access",
        },
        _access_token_lifetime(),
    )


def create_refresh_token(*, user_id: uuid.UUID) -> tuple[str, str]:
    """Returns (token, jti). The jti is what logout adds to the Redis denylist."""
    jti = str(uuid.uuid4())
    token = _encode(
        {"sub": str(user_id), "jti": jti, "typ": "refresh"},
        _refresh_token_lifetime(),
    )
    return token, jti


def decode_claims(token: str, *, expected_type: str) -> dict[str, Any]:
    """Verify signature, expiry and `typ`, and return the raw claims.

    The sibling of `decode_token` for a token whose claims are not a
    `TokenPayload` -- the widget's visitor token (`app/widget/tokens.py`)
    carries `agent`/`vid` and no `sub`/`jti`. Same secret, same algorithm,
    same `typ` rule, so a token of one type is refused as every other type
    whichever of the two decoders reads it.
    """
    try:
        raw: dict[str, Any] = jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthenticationError("invalid or expired token") from exc

    if raw.get("typ") != expected_type:
        raise AuthenticationError("invalid or expired token")
    return raw


def decode_token(token: str, *, expected_type: str) -> TokenPayload:
    raw = decode_claims(token, expected_type=expected_type)
    try:
        return TokenPayload.model_validate(raw)
    except ValidationError as exc:
        raise AuthenticationError("invalid or expired token") from exc


def refresh_token_ttl_seconds() -> int:
    return int(_refresh_token_lifetime().total_seconds())
