from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.tenancy import TenantContext


def _settings():
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_pubsub_push_audience="https://api.example.com/api/v1/gmail/pubsub",
        gmail_pubsub_expected_service_account="push@example.iam.gserviceaccount.com",
    )


def _push_body(email: str = "Quotes@example.com", history_id: str = "42") -> bytes:
    data = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    return json.dumps(
        {
            "message": {
                "messageId": "push-1",
                "data": base64.b64encode(data).decode("ascii"),
            }
        }
    ).encode()


def _webhook_app() -> FastAPI:
    from app.api.pubsub import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.anyio
async def test_authenticated_push_enqueues_normalized_mailbox_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import pubsub as pubsub_api

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(pubsub_api, "get_settings", _settings)
    monkeypatch.setattr(pubsub_api, "verify_pubsub_request", lambda *_a, **_k: {})

    async def enqueue(email: str, history_id: str) -> bool:
        queued.append((email, history_id))
        return True

    monkeypatch.setattr(pubsub_api, "enqueue_gmail_sync", enqueue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_webhook_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/gmail/pubsub",
            content=_push_body(),
            headers={"Authorization": "Bearer signed.jwt"},
        )

    assert response.status_code == 204
    assert queued == [("quotes@example.com", "42")]


@pytest.mark.anyio
async def test_invalid_push_identity_is_rejected_before_payload_decode_or_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import pubsub as pubsub_api
    from app.gmail.pubsub import PubSubAuthError

    monkeypatch.setattr(pubsub_api, "get_settings", _settings)

    def reject(*_args: object, **_kwargs: object) -> None:
        raise PubSubAuthError("bad identity")

    def should_not_decode(_body: bytes) -> object:
        raise AssertionError("unauthenticated body must not be decoded")

    async def should_not_enqueue(*_args: object) -> bool:
        raise AssertionError("unauthenticated push must not enqueue")

    monkeypatch.setattr(pubsub_api, "verify_pubsub_request", reject)
    monkeypatch.setattr(pubsub_api, "decode_pubsub_envelope", should_not_decode)
    monkeypatch.setattr(pubsub_api, "enqueue_gmail_sync", should_not_enqueue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_webhook_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/gmail/pubsub",
            content=b"not-json",
            headers={"Authorization": "Bearer forged"},
        )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_mailbox_sync_resolves_only_the_matching_tenant_before_refreshing_tokens(
    monkeypatch: pytest.MonkeyPatch,
    owner_connection: Any,
    tenant_a: TenantContext,
    tenant_b: TenantContext,
) -> None:
    import base64 as base64_stdlib

    from app.core.config import Settings
    from app.core.ids import uuid7
    from app.gmail.client import GmailClient
    from app.gmail.oauth import GmailOAuthClient, OAuthTokens
    from app.gmail.sync import run_gmail_sync
    from app.gmail.token_crypto import encrypt_token

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        gmail_token_encryption_key=base64_stdlib.b64encode(b"t" * 32).decode(),
    )
    monkeypatch.setattr("app.gmail.token_crypto.get_settings", lambda: settings)
    monkeypatch.setattr("app.gmail.oauth.get_settings", lambda: settings)
    monkeypatch.setattr("app.gmail.client.get_settings", lambda: settings)
    connection_a, connection_b = uuid7(), uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO gmail_connections "
            "(id, organization_id, email, refresh_token_ciphertext, history_id, status) "
            "VALUES (:id, :org, :email, :token, '100', 'connected')"
        ),
        {
            "id": connection_a,
            "org": tenant_a.organization_id,
            "email": "quotes-a@example.com",
            "token": encrypt_token("tenant-a-token", key=b"t" * 32),
        },
    )
    await owner_connection.execute(
        text(
            "INSERT INTO gmail_connections "
            "(id, organization_id, email, refresh_token_ciphertext, history_id, status) "
            "VALUES (:id, :org, :email, :token, '100', 'connected')"
        ),
        {
            "id": connection_b,
            "org": tenant_b.organization_id,
            "email": "quotes-b@example.com",
            "token": encrypt_token("tenant-b-token", key=b"t" * 32),
        },
    )
    await owner_connection.commit()
    observed_refresh_tokens: list[str] = []

    class _Lock:
        async def acquire(self, *, blocking: bool) -> bool:
            return blocking is False

        async def release(self) -> None:
            return None

    class _Redis:
        def lock(self, *_args: object, **_kwargs: object) -> _Lock:
            return _Lock()

    async def refresh(_self: GmailOAuthClient, token: str, **_kwargs: object) -> OAuthTokens:
        observed_refresh_tokens.append(token)
        return OAuthTokens(access_token="access-token")

    async def history(
        _self: GmailClient,
        _token: str,
        _cursor: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {"history": [], "historyId": "101"}

    monkeypatch.setattr("app.gmail.sync.get_redis", lambda: _Redis())
    monkeypatch.setattr(GmailOAuthClient, "refresh_access_token", refresh)
    monkeypatch.setattr(GmailClient, "list_history", history)

    await run_gmail_sync(
        mailbox_email="quotes-a@example.com",
        notification_history_id="101",
    )

    assert observed_refresh_tokens == ["tenant-a-token"]
