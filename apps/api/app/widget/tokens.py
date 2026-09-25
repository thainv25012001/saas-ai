"""Anonymous widget visitor tokens
(docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §4).

HS256 with the existing `JWT_SECRET`, claims `{typ: "widget", org, agent,
vid, iat, exp}`. `typ` is what keeps the two token worlds apart: a widget
token is refused wherever an access token is expected
(`app.core.security.decode_token` checks `typ`), and an access or refresh
token is refused here. `vid` is a random UUID4 string with no link to any
person; it is the only visitor identity the system has.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import decode_claims, encode_with_expiry

WIDGET_TOKEN_TYPE = "widget"


@dataclass(frozen=True, slots=True)
class VisitorClaims:
    organization_id: uuid.UUID
    agent_id: uuid.UUID
    visitor_id: str
    expires_at: datetime


class _WidgetTokenPayload(BaseModel):
    org: uuid.UUID
    agent: uuid.UUID
    # Always a UUID4 string when minted here; bounded rather than typed as a
    # UUID so the check is "what we sign", not a second format rule.
    vid: str = Field(min_length=1, max_length=64)
    exp: int


def _token_lifetime() -> timedelta:
    return timedelta(days=get_settings().widget_token_days)


def create_visitor_token(
    *, organization_id: uuid.UUID, agent_id: uuid.UUID, visitor_id: str
) -> tuple[str, datetime]:
    """Returns the token and the exact expiry it carries."""
    return encode_with_expiry(
        {
            "typ": WIDGET_TOKEN_TYPE,
            "org": str(organization_id),
            "agent": str(agent_id),
            "vid": visitor_id,
        },
        _token_lifetime(),
    )


def decode_visitor_token(token: str) -> VisitorClaims:
    """`AuthenticationError` for a bad signature, an expired token, a token of
    any other `typ`, or a widget-typed token missing a claim."""
    raw = decode_claims(token, expected_type=WIDGET_TOKEN_TYPE)
    try:
        payload = _WidgetTokenPayload.model_validate(raw)
    except PydanticValidationError as exc:
        raise AuthenticationError("invalid or expired token") from exc
    return VisitorClaims(
        organization_id=payload.org,
        agent_id=payload.agent,
        visitor_id=payload.vid,
        expires_at=datetime.fromtimestamp(payload.exp, UTC),
    )
