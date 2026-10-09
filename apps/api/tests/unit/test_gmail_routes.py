from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


async def test_oauth_start_returns_google_consent_url_with_readonly_scope(
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
        response = await client.post("/api/v1/gmail/oauth/start")

    # JSON, not a redirect: the dashboard calls this with its Bearer token and
    # then navigates itself, since a browser navigation cannot carry the header.
    assert response.status_code == 200, response.text
    location = response.json()["authorization_url"]
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
        response = await client.post("/api/v1/gmail/oauth/start")

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
        gmail_sync_mode="pubsub",
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
        transport=httpx.ASGITransport(
            app=_test_app(
                TenantContext(
                    organization_id=organization_id,
                    user_id=user_id,
                    role=MembershipRole.OWNER,
                    request_id="test",
                )
            )
        ),
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


async def test_supplier_reprocess_is_enqueued_only_after_tenant_transaction_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api

    organization_id, message_id, supplier_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mapping_id, connection_id = uuid.uuid4(), uuid.uuid4()
    committed = False
    queued: list[tuple[str, str, str]] = []

    class _Result:
        def scalar_one_or_none(self) -> object:
            return type(
                "MessageRow",
                (),
                {
                    "id": message_id,
                    "supplier_id": supplier_id,
                    "gmail_connection_id": connection_id,
                },
            )()

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

        async def scalar(self, _statement: object) -> object:
            if not hasattr(self, "mapping_seen"):
                self.mapping_seen = True
                return mapping_id
            return type("ConnectionRow", (), {"status": "connected"})()

    @asynccontextmanager
    async def fake_session(_tenant: TenantContext):
        nonlocal committed
        yield _Session()
        committed = True

    async def enqueue(org_id: str, source_message_id: str, confirmed_mapping_id: str) -> None:
        assert committed is True
        queued.append((org_id, source_message_id, confirmed_mapping_id))

    monkeypatch.setattr(gmail_api, "tenant_session", fake_session)
    monkeypatch.setattr(gmail_api, "enqueue_supplier_message_reprocess", enqueue)
    tenant = TenantContext(
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        role=MembershipRole.OWNER,
        request_id="test",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/gmail/supplier-messages/{message_id}/reprocess")

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "queued"}
    assert queued == [(str(organization_id), str(message_id), str(mapping_id))]


async def test_reconnecting_a_disconnected_mailbox_starts_from_the_new_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api
    from app.core.config import Settings
    from app.db.models import GmailConnection
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
        gmail_sync_mode="pubsub",
    )
    redis = _FakeRedis()
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_api, "get_redis", lambda: redis)
    monkeypatch.setattr("app.gmail.token_crypto.get_settings", lambda: settings)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    await OAuthStateStore(redis).issue(organization_id, user_id)
    state = next(iter(redis.values)).removeprefix("gmail:oauth:state:")
    existing = GmailConnection(
        organization_id=organization_id,
        email="supplierinbox@example.com",
        refresh_token_ciphertext=None,
        history_id="5",
        status="disconnected",
    )

    class _Result:
        def first(self) -> tuple[str]:
            return ("owner",)

        def scalar_one_or_none(self) -> GmailConnection:
            return existing

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

        def add(self, _record: object) -> None:
            raise AssertionError("the existing row is reused")

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
    tenant = TenantContext(
        organization_id=organization_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="test",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/gmail/oauth/callback", params={"code": "oauth-code", "state": state}
        )

    assert "gmail=connected" in response.headers["location"]
    # Mail that arrived while disconnected is not backfilled.
    assert existing.history_id == "18"
    assert existing.status == "connected"
    assert existing.refresh_token_ciphertext is not None
    assert decrypt_token(existing.refresh_token_ciphertext) == "secret-refresh"


def _gmail_settings(**overrides: object):
    from app.core.config import Settings

    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://app_user:password@localhost:5432/app",
        "migration_database_url": "postgresql+asyncpg://app_owner:password@localhost:5432/app",
        "jwt_secret": "unit-test-secret-that-is-long-enough",
        "gmail_oauth_client_id": "client-id",
        "gmail_oauth_client_secret": "client-secret",
        "gmail_oauth_redirect_uri": "https://api.example.com/api/v1/gmail/oauth/callback",
        "gmail_web_redirect_uri": "https://app.example.com/dashboard/supplier-quotes",
        "gmail_token_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class _CallbackRun:
    response: httpx.Response | None
    added: list[object]
    watch_calls: list[str]
    stop_calls: list[str]


async def _run_callback(
    monkeypatch: pytest.MonkeyPatch,
    settings: object,
    *,
    existing: object | None = None,
    fail_save: bool = False,
) -> _CallbackRun:
    """Drive the OAuth callback with Google and the database faked.

    The profile reports `historyId` 17 and a watch would report 18, so a
    test can tell which one became the cursor. With `fail_save` the row
    write raises and `response` is None.
    """
    from app.api import gmail as gmail_api
    from app.gmail.client import GmailClient, GmailProfile, GmailWatchResult
    from app.gmail.oauth import GmailOAuthClient, OAuthStateStore, OAuthTokens

    redis = _FakeRedis()
    monkeypatch.setattr(gmail_api, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_api, "get_redis", lambda: redis)
    monkeypatch.setattr("app.gmail.token_crypto.get_settings", lambda: settings)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    await OAuthStateStore(redis).issue(organization_id, user_id)
    state = next(iter(redis.values)).removeprefix("gmail:oauth:state:")
    added: list[object] = []
    watch_calls: list[str] = []
    stop_calls: list[str] = []

    class _Result:
        def first(self) -> tuple[str]:
            return ("owner",)

        def scalar_one_or_none(self) -> object | None:
            return existing

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

        def add(self, record: object) -> None:
            if fail_save:
                raise RuntimeError("database unavailable")
            added.append(record)

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    monkeypatch.setattr(gmail_api, "untenanted_session", fake_session)
    monkeypatch.setattr(gmail_api, "tenant_session", lambda _tenant: fake_session())

    async def exchange(*_args: object, **_kwargs: object) -> OAuthTokens:
        return OAuthTokens(access_token="secret-access", refresh_token="secret-refresh")

    async def profile(_self: GmailClient, _token: str) -> GmailProfile:
        return GmailProfile(emailAddress="SupplierInbox@example.com", historyId="17")

    async def watch(_self: GmailClient, _token: str, topic: str) -> GmailWatchResult:
        watch_calls.append(topic)
        return GmailWatchResult(
            history_id="18", expiration=datetime.fromtimestamp(1_900_000_000, tz=UTC)
        )

    async def stop_watch(_self: GmailClient, token: str) -> None:
        stop_calls.append(token)

    monkeypatch.setattr(GmailOAuthClient, "exchange_code", exchange)
    monkeypatch.setattr(GmailClient, "get_profile", profile)
    monkeypatch.setattr(GmailClient, "watch", watch)
    monkeypatch.setattr(GmailClient, "stop_watch", stop_watch)
    tenant = TenantContext(
        organization_id=organization_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="test",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_test_app(tenant)),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        try:
            response: httpx.Response | None = await client.get(
                "/api/v1/gmail/oauth/callback", params={"code": "oauth-code", "state": state}
            )
        except RuntimeError:
            if not fail_save:
                raise
            response = None
    return _CallbackRun(response, added, watch_calls, stop_calls)


@pytest.mark.parametrize(
    ("mode", "topic", "expected_status"),
    [
        ("polling", None, 200),
        ("pubsub", None, 422),
        ("pubsub", "projects/test/topics/gmail", 200),
    ],
)
async def test_oauth_start_needs_a_topic_only_in_pubsub_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, topic: str | None, expected_status: int
) -> None:
    from app.api import gmail as gmail_api

    settings = _gmail_settings(gmail_sync_mode=mode, gmail_pubsub_topic=topic)
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
        transport=httpx.ASGITransport(app=_test_app(tenant)), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/gmail/oauth/start")

    assert response.status_code == expected_status, response.text


@pytest.mark.parametrize("topic", [None, "projects/test/topics/gmail"])
async def test_polling_connect_starts_from_the_profile_cursor_without_a_watch(
    monkeypatch: pytest.MonkeyPatch, topic: str | None
) -> None:
    settings = _gmail_settings(gmail_sync_mode="polling", gmail_pubsub_topic=topic)

    run = await _run_callback(monkeypatch, settings)

    assert run.response is not None
    assert "gmail=connected" in run.response.headers["location"]
    # A topic left in .env from Pub/Sub days must not start a watch.
    assert run.watch_calls == []
    (saved,) = run.added
    assert saved.history_id == "17"
    assert saved.watch_expires_at is None
    assert saved.status == "connected"


async def test_polling_reconnect_after_needs_reauth_keeps_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.models import GmailConnection

    existing = GmailConnection(
        email="supplierinbox@example.com",
        refresh_token_ciphertext=None,
        history_id="5",
        status="needs_reauth",
        last_error_code="gmail_refresh_failed",
    )

    run = await _run_callback(
        monkeypatch, _gmail_settings(gmail_sync_mode="polling"), existing=existing
    )

    assert run.response is not None
    assert "gmail=connected" in run.response.headers["location"]
    # Mail that arrived while the token was broken is still ahead of cursor 5;
    # resetting to the profile's 17 would skip it.
    assert existing.history_id == "5"
    assert existing.status == "connected"
    assert existing.last_error_code is None
    assert existing.watch_expires_at is None
    assert run.watch_calls == []


async def test_a_failed_polling_connect_never_stops_a_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_callback(
        monkeypatch, _gmail_settings(gmail_sync_mode="polling"), fail_save=True
    )

    assert run.response is None
    # This callback started no watch; one on this mailbox from Pub/Sub days
    # is not this callback's to stop.
    assert run.stop_calls == []


async def test_a_failed_pubsub_connect_stops_the_watch_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _gmail_settings(
        gmail_sync_mode="pubsub", gmail_pubsub_topic="projects/test/topics/gmail"
    )

    run = await _run_callback(monkeypatch, settings, fail_save=True)

    assert run.response is None
    assert run.watch_calls == ["projects/test/topics/gmail"]
    assert run.stop_calls == ["secret-access"]
