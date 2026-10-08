from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.redis import get_redis

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_PREFIX = "gmail:oauth:state:"
_CONSUME_STATE_SCRIPT = (
    "local value = redis.call('GET', KEYS[1]); "
    "if value then redis.call('DEL', KEYS[1]); end; "
    "return value"
)


class OAuthStateError(ValueError):
    """OAuth state is missing, expired, replayed, or bound to another user."""


class OAuthStateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    code_verifier: str


class OAuthStateContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    user_id: uuid.UUID
    code_verifier: str = Field(min_length=43, max_length=128)


class OAuthStateStore:
    def __init__(self, redis: Any | None = None, *, ttl_seconds: float = 600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("OAuth state TTL must be positive")
        self.redis = redis if redis is not None else get_redis()
        self.ttl_milliseconds = max(1, int(ttl_seconds * 1000))

    async def issue(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> OAuthStateTicket:
        for _ in range(3):
            state = secrets.token_urlsafe(32)
            code_verifier = secrets.token_urlsafe(64)
            payload = json.dumps(
                {
                    "organization_id": str(organization_id),
                    "user_id": str(user_id),
                    "code_verifier": code_verifier,
                },
                separators=(",", ":"),
            )
            created = await self.redis.set(
                f"{_STATE_PREFIX}{state}",
                payload,
                px=self.ttl_milliseconds,
                nx=True,
            )
            if created:
                return OAuthStateTicket(state=state, code_verifier=code_verifier)
        raise RuntimeError("could not create Gmail OAuth state")

    async def consume(
        self,
        state: str,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> OAuthStateContext:
        context = await self.consume_for_callback(state)
        if context.organization_id != organization_id or context.user_id != user_id:
            raise OAuthStateError("Gmail OAuth state does not match the authenticated user")
        return context

    async def consume_for_callback(self, state: str) -> OAuthStateContext:
        """Atomically load a callback ticket; its random state is the callback credential."""
        eval_script = cast(Callable[..., Awaitable[Any]], self.redis.eval)
        raw = await eval_script(
            _CONSUME_STATE_SCRIPT,
            1,
            f"{_STATE_PREFIX}{state}",
        )
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            raise OAuthStateError("Gmail OAuth state is invalid or expired")
        try:
            context = OAuthStateContext.model_validate_json(raw)
        except (ValueError, TypeError) as exc:
            raise OAuthStateError("Gmail OAuth state is invalid or expired") from exc
        return context


class OAuthTokens(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_in: int = Field(default=3600, ge=0)
    token_type: str = "Bearer"


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_verifier: str,
) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
    parameters = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge.decode("ascii").rstrip("="),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(parameters)}"


class GmailOAuthClient:
    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        token_url: str = GOOGLE_TOKEN_URL,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.http_client = http_client
        self.token_url = token_url

    async def exchange_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokens:
        return await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh_access_token(
        self,
        refresh_token: str,
        *,
        previous_refresh_token: str | None = None,
    ) -> OAuthTokens:
        tokens = await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        if tokens.refresh_token is None:
            tokens.refresh_token = previous_refresh_token or refresh_token
        return tokens

    async def _request_tokens(self, grant: Mapping[str, str]) -> OAuthTokens:
        client_id, client_secret = self._credentials()
        form = {
            **grant,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        try:
            if self.http_client is not None:
                response = await self.http_client.post(self.token_url, data=form)
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(self.token_url, data=form)
        except httpx.RequestError as exc:
            raise RuntimeError("Google OAuth request failed") from exc
        if response.is_error:
            raise RuntimeError(f"Google OAuth request failed with HTTP {response.status_code}")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            return OAuthTokens.model_validate(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Google OAuth returned an invalid token response") from exc

    def _credentials(self) -> tuple[str, str]:
        client_id = self.client_id
        client_secret = self.client_secret
        if not client_id or not client_secret:
            settings = get_settings()
            client_id = client_id or settings.gmail_oauth_client_id
            client_secret = client_secret or settings.gmail_oauth_client_secret
        if not client_id or not client_secret:
            raise RuntimeError("Gmail OAuth is not configured")
        return client_id, client_secret
