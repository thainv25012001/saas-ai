import uuid
from datetime import UTC, datetime, timedelta

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


def _encode(claims: dict[str, object], lifetime: timedelta) -> str:
    payload = {
        **claims,
        "exp": int((datetime.now(UTC) + lifetime).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=_ALGORITHM)


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


def decode_token(token: str, *, expected_type: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
        payload = TokenPayload.model_validate(raw)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise AuthenticationError("invalid or expired token") from exc

    if payload.typ != expected_type:
        raise AuthenticationError("invalid or expired token")
    return payload


def refresh_token_ttl_seconds() -> int:
    return int(_refresh_token_lifetime().total_seconds())
