from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.gmail.oauth import (
    GmailOAuthClient,
    GmailOAuthTransientError,
    OAuthStateError,
    OAuthStateStore,
    build_authorization_url,
)

pytestmark = pytest.mark.anyio


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}

    async def set(self, key: str, value: str, *, px: int, nx: bool) -> bool:
        if nx and key in self.values and self.values[key][1] > time.monotonic():
            return False
        self.values[key] = (value, time.monotonic() + px / 1000)
        return True

    async def eval(self, _script: str, _keys: int, key: str) -> str | None:
        entry = self.values.pop(key, None)
        if entry is None or entry[1] <= time.monotonic():
            return None
        return entry[0]


async def test_gmail_settings_are_optional_and_have_bounded_mvp_defaults() -> None:
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
    )

    assert settings.gmail_oauth_client_id is None
    assert settings.gmail_oauth_client_secret is None
    assert settings.gmail_token_encryption_key is None
    assert settings.gmail_attachment_max_bytes == 20 * 1024 * 1024
    assert settings.gmail_workbook_max_rows == 25_000
    assert settings.gmail_preview_rows == 5
    assert settings.gmail_sale_price_max_age_days == 90


async def test_oauth_state_expires_is_one_time_and_is_bound_to_user() -> None:
    redis = _FakeRedis()
    store = OAuthStateStore(redis, ttl_seconds=0.02)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    issued = await store.issue(organization_id, user_id)

    state = await store.consume(
        issued.state,
        organization_id=organization_id,
        user_id=user_id,
    )
    assert state.code_verifier == issued.code_verifier
    with pytest.raises(OAuthStateError):
        await store.consume(
            issued.state,
            organization_id=organization_id,
            user_id=user_id,
        )

    expired = await store.issue(organization_id, user_id)
    await asyncio.sleep(0.03)
    with pytest.raises(OAuthStateError):
        await store.consume(
            expired.state,
            organization_id=organization_id,
            user_id=user_id,
        )


async def test_oauth_state_mismatch_is_consumed_and_rejected() -> None:
    store = OAuthStateStore(_FakeRedis())
    issued = await store.issue(uuid.uuid4(), uuid.uuid4())

    with pytest.raises(OAuthStateError):
        await store.consume(
            issued.state,
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )


async def test_callback_state_resolves_bound_user_context_only_once() -> None:
    store = OAuthStateStore(_FakeRedis())
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    issued = await store.issue(organization_id, user_id)

    context = await store.consume_for_callback(issued.state)
    assert context.organization_id == organization_id
    assert context.user_id == user_id
    assert context.code_verifier == issued.code_verifier
    with pytest.raises(OAuthStateError):
        await store.consume_for_callback(issued.state)


def test_authorization_url_requests_only_gmail_readonly_and_uses_pkce() -> None:
    verifier = "sample-code-verifier"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    url = build_authorization_url(
        client_id="client-id",
        redirect_uri="https://api.example.com/api/v1/gmail/oauth/callback",
        state="opaque-state",
        code_verifier=verifier,
    )
    query = parse_qs(urlparse(url).query)

    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert query["state"] == ["opaque-state"]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]


async def test_refresh_keeps_stored_refresh_token_when_google_omits_a_new_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        assert request.content.decode().find("refresh_token=stored-refresh") >= 0
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://oauth2.googleapis.com"
    ) as http_client:
        oauth = GmailOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        tokens = await oauth.refresh_access_token(
            "stored-refresh", previous_refresh_token="stored-refresh"
        )

    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "stored-refresh"


async def test_oauth_provider_error_does_not_echo_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "private-token-echo"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://oauth2.googleapis.com"
    ) as http_client:
        oauth = GmailOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        with pytest.raises(RuntimeError) as error:
            await oauth.exchange_code(
                "secret-code", redirect_uri="https://callback", code_verifier="v"
            )

    assert "private-token-echo" not in str(error.value)
    assert "secret-code" not in str(error.value)


async def _refresh_with(handler: httpx.MockTransport) -> None:
    async with httpx.AsyncClient(
        transport=handler, base_url="https://oauth2.googleapis.com"
    ) as http_client:
        oauth = GmailOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        await oauth.refresh_access_token("stored-refresh")


async def test_a_network_error_during_refresh_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    with pytest.raises(GmailOAuthTransientError):
        await _refresh_with(httpx.MockTransport(handler))


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_a_throttled_or_failing_google_is_transient(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "try_later"})

    with pytest.raises(GmailOAuthTransientError):
        await _refresh_with(httpx.MockTransport(handler))


@pytest.mark.parametrize("status_code", [400, 401])
async def test_a_rejected_refresh_token_is_not_transient(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "invalid_grant"})

    with pytest.raises(RuntimeError) as error:
        await _refresh_with(httpx.MockTransport(handler))

    assert not isinstance(error.value, GmailOAuthTransientError)
