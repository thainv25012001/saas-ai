from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.auth.dependencies import get_current_tenant
from app.core.errors import AppError
from app.core.tenancy import TenantContext
from app.db.models import MembershipRole

pytestmark = pytest.mark.anyio


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, *_args: object, **_kwargs: object) -> bool:
        key, value = _args[:2]
        self.values[str(key)] = str(value)
        return True

    async def eval(self, _script: str, _keys: int, key: str) -> str | None:
        return self.values.pop(key, None)


def _test_app(tenant: TenantContext) -> FastAPI:
    from app.api.gmail import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_tenant] = lambda: tenant

    @app.exception_handler(AppError)
    async def app_error_handler(_request: object, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    return app


async def test_oauth_start_redirects_owner_to_google_with_readonly_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_oauth_client_id="client-id",
        gmail_oauth_client_secret="client-secret",
        gmail_oauth_redirect_uri="https://api.example.com/api/v1/gmail/oauth/callback",
        gmail_web_redirect_uri="https://app.example.com/dashboard/supplier-quotes",
        gmail_token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        gmail_pubsub_topic="projects/test/topics/gmail",
    )
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.gmail.token_crypto.get_settings", lambda: settings)
    monkeypatch.setattr(gmail_api, "get_redis", lambda: _FakeRedis())
    tenant = TenantContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=MembershipRole.OWNER,
        request_id="test",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/v1/gmail/oauth/start")

    assert response.status_code == 307, response.text
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly" in location
    assert "code_challenge_method=S256" in location
    assert "client-secret" not in location


async def test_non_owner_cannot_start_gmail_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import gmail as gmail_api
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_oauth_client_id="client-id",
        gmail_oauth_client_secret="client-secret",
        gmail_oauth_redirect_uri="https://api.example.com/callback",
        gmail_web_redirect_uri="https://app.example.com/dashboard/supplier-quotes",
        gmail_token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        gmail_pubsub_topic="projects/test/topics/gmail",
    )
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    tenant = TenantContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=MembershipRole.MEMBER,
        request_id="test",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/gmail/oauth/start")

    assert response.status_code == 403


async def test_invalid_callback_state_never_exchanges_oauth_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_web_redirect_uri="https://app.example.com/dashboard/supplier-quotes",
    )
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_api, "get_redis", lambda: _FakeRedis())

    async def fail_exchange(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("OAuth exchange must not run for invalid state")

    monkeypatch.setattr(gmail_api.GmailOAuthClient, "exchange_code", fail_exchange)
    tenant = TenantContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=MembershipRole.OWNER,
        request_id="test",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/gmail/oauth/callback", params={"code": "secret-code", "state": "invalid"}
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "https://app.example.com/dashboard/supplier-quotes?gmail=error"
    )


async def test_valid_callback_saves_only_encrypted_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api
    from app.core.config import Settings
    from app.gmail.client import GmailClient, GmailProfile, GmailWatchResult
    from app.gmail.oauth import GmailOAuthClient, OAuthStateStore, OAuthTokens
    from app.gmail.token_crypto import decrypt_token

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_oauth_client_id="client-id",
        gmail_oauth_client_secret="client-secret",
        gmail_oauth_redirect_uri="https://api.example.com/api/v1/gmail/oauth/callback",
        gmail_web_redirect_uri="https://app.example.com/dashboard/supplier-quotes",
        gmail_token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        gmail_pubsub_topic="projects/test/topics/gmail",
    )
    redis = _FakeRedis()
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_api, "get_redis", lambda: redis)
    monkeypatch.setattr("app.gmail.token_crypto.get_settings", lambda: settings)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    await OAuthStateStore(redis).issue(organization_id, user_id)
    state = next(iter(redis.values)).removeprefix("gmail:oauth:state:")
    saved_connections: list[object] = []

    class _Result:
        def first(self) -> tuple[str]:
            return ("owner",)

        def scalar_one_or_none(self) -> None:
            return None

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

        def add(self, record: object) -> None:
            saved_connections.append(record)

        async def delete(self, _record: object) -> None:
            return None

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    monkeypatch.setattr(gmail_api, "untenanted_session", fake_session)
    monkeypatch.setattr(gmail_api, "tenant_session", lambda _tenant: fake_session())

    async def exchange(*_args: object, **_kwargs: object) -> OAuthTokens:
        return OAuthTokens(access_token="secret-access", refresh_token="secret-refresh")

    async def profile(_self: GmailClient, _token: str) -> GmailProfile:
        return GmailProfile(emailAddress="SupplierInbox@example.com", historyId="17")

    async def watch(_self: GmailClient, _token: str, _topic: str) -> GmailWatchResult:
        return GmailWatchResult(
            history_id="18", expiration=datetime.fromtimestamp(1_900_000_000, tz=UTC)
        )

    monkeypatch.setattr(GmailOAuthClient, "exchange_code", exchange)
    monkeypatch.setattr(GmailClient, "get_profile", profile)
    monkeypatch.setattr(GmailClient, "watch", watch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(
            TenantContext(
                organization_id=organization_id,
                user_id=user_id,
                role=MembershipRole.OWNER,
                request_id="test",
            )
        )),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/gmail/oauth/callback", params={"code": "oauth-code", "state": state}
        )

    assert response.status_code == 303
    assert "gmail=connected" in response.headers["location"]
    assert "secret-access" not in response.headers["location"]
    assert len(saved_connections) == 1
    saved = saved_connections[0]
    assert saved.email == "supplierinbox@example.com"
    assert saved.refresh_token_ciphertext != "secret-refresh"
    assert decrypt_token(saved.refresh_token_ciphertext) == "secret-refresh"
