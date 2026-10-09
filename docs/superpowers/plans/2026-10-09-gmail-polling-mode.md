# Gmail Polling Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pick up new supplier mail by polling Gmail every few minutes, as the default alternative to Pub/Sub push.

**Architecture:** A setting chooses the mode. In `polling` mode an arq cron job fans out one `poll_gmail_mailbox_task` per connected mailbox, each calling the existing `run_gmail_sync` from the stored history cursor. Connecting Gmail takes its starting cursor from the Gmail profile instead of `users.watch`. `pubsub` mode keeps today's webhook and the 03:00 watch renewal, unchanged.

**Tech Stack:** Python 3.12, FastAPI, pydantic-settings, SQLAlchemy async, arq 0.28.0 (Redis), pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-10-09-gmail-polling-mode-design.md`

## Global Constraints

- `GMAIL_SYNC_MODE` accepts exactly `pubsub` or `polling`; the default is `polling`.
- `GMAIL_POLL_INTERVAL_MINUTES` defaults to `5`, must be between 1 and 60, and must divide 60.
- Poll job ID: `gmail-poll:` + the first 40 hex characters of sha256 of the stripped, case-folded address.
- `poll_gmail_mailbox_task` is registered with `keep_result=0, max_tries=3`. The poll cron has `max_tries=1`, `timeout=60`, `run_at_startup=True`.
- The Pub/Sub path is unchanged: `run_gmail_sync(skip_if_busy=False)` still raises `Retry` on a busy mailbox, and the webhook route stays as is.
- No new dependencies, no migrations, no GraphQL or UI changes.
- arq is pinned at 0.28.0 (`apps/api/uv.lock`). The dedup design relies on its `Worker.finish_job` deleting the job key when no result is kept.
- **Running tests from the host.** `.env` points at docker hostnames, so every pytest command runs from `apps/api` with these three variables inline (each Bash call is a fresh shell):
  `DATABASE_URL=postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai MIGRATION_DATABASE_URL=postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai REDIS_URL=redis://localhost:6379/0`
  The commands below write this as `$HOST_ENV`; paste the three assignments in its place.
- Integration tests write into the dev database `saas_ai`; there is no separate test DB. Tell the user before the first integration run in case they are using it.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`

## Review Focus

1. A developer's `.env` still sets `GMAIL_PUBSUB_TOPIC` while the mode is `polling`. Connecting must not start a watch. Pinned in Task 2 (`test_polling_connect_starts_from_the_profile_cursor_without_a_watch`, topic parametrized).
2. An owner reconnects a `needs_reauth` mailbox in polling mode. The cursor must be kept so mail from the broken period is caught up, not skipped. Pinned in Task 2 (`test_polling_reconnect_after_needs_reauth_keeps_the_cursor`).
3. Saving a polling-mode connection fails after Google approved it. No `stop_watch` may be sent: this callback started no watch, and a Pub/Sub-era watch on the mailbox may still be serving someone. Pinned in Task 2 (`test_a_failed_polling_connect_never_stops_a_watch`).
4. A poll job that crashes or exhausts its retries must not hold its mailbox's fixed job ID, or that mailbox would never be polled again. Pinned in Task 5 with a real arq worker (`test_a_finished_poll_frees_its_mailbox_for_the_next_interval`, success, failure and retries exhausted). An exhausted poll leaves an hour-long `arq:result:` key, so the fan-out deletes that key before each enqueue.
5. A fresh deployment boots with no connected mailbox. The cron must run cleanly and queue nothing. Pinned in Task 5 (`test_a_poll_with_no_connected_mailboxes_queues_nothing`).

---

### Task 1: Sync mode and poll interval settings

**Files:**
- Modify: `apps/api/app/core/config.py` (imports at line 3; Gmail settings near line 117; validators after `reject_non_tcp_redis_url`)
- Modify: `docker-compose.yml` (the two `# ---- Phase 9 (Gmail supplier quotes)` blocks, `api` and `worker`)
- Modify: `.env.example` (Gmail section)
- Test: `apps/api/tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.gmail_sync_mode: Literal["pubsub", "polling"]` (default `"polling"`), `Settings.gmail_poll_interval_minutes: int` (default `5`).

- [ ] **Step 1: Write the failing tests**

At the top of `apps/api/tests/unit/test_config.py`, make the imports read:

```python
import pytest
from pydantic import ValidationError

from app.core.config import _find_env_file, _normalize_database_url, get_settings
```

Append to the end of the file:

```python
def _settings(**overrides: object):
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        **overrides,
    )


def test_gmail_polls_every_five_minutes_by_default(monkeypatch):
    monkeypatch.delenv("GMAIL_SYNC_MODE", raising=False)
    monkeypatch.delenv("GMAIL_POLL_INTERVAL_MINUTES", raising=False)

    settings = _settings()

    assert settings.gmail_sync_mode == "polling"
    assert settings.gmail_poll_interval_minutes == 5


def test_gmail_sync_mode_and_interval_read_the_environment(monkeypatch):
    monkeypatch.setenv("GMAIL_SYNC_MODE", "pubsub")
    monkeypatch.setenv("GMAIL_POLL_INTERVAL_MINUTES", "15")

    settings = _settings()

    assert settings.gmail_sync_mode == "pubsub"
    assert settings.gmail_poll_interval_minutes == 15


def test_gmail_sync_mode_rejects_anything_but_pubsub_or_polling():
    with pytest.raises(ValidationError):
        _settings(gmail_sync_mode="push")


@pytest.mark.parametrize("minutes", [1, 2, 5, 10, 15, 30, 60])
def test_poll_interval_accepts_divisors_of_an_hour(minutes):
    assert _settings(gmail_poll_interval_minutes=minutes).gmail_poll_interval_minutes == minutes


@pytest.mark.parametrize("minutes", [0, -5, 7, 45, 61, 120])
def test_poll_interval_rejects_values_that_do_not_divide_an_hour(minutes):
    # The cron runs at minutes range(0, 60, interval): 7 would leave a
    # 4-minute gap at every hour (56 -> 0), 45 a 15-minute one.
    with pytest.raises(ValidationError):
        _settings(gmail_poll_interval_minutes=minutes)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_config.py -v -k "gmail or poll_interval"`
Expected: FAIL. The default test fails with `AttributeError: 'Settings' object has no attribute 'gmail_sync_mode'`, and the rejection tests fail with `DID NOT RAISE`.

- [ ] **Step 3: Add the settings**

In `apps/api/app/core/config.py` change `from typing import Annotated` to:

```python
from typing import Annotated, Literal
```

Directly below `gmail_oauth_state_ttl_seconds: int = Field(default=600, gt=0)` add:

```python
    # How new mail is noticed (docs/PHASE-9.md §2). `polling` reads each
    # connected mailbox's history every `gmail_poll_interval_minutes` and
    # needs no Google Cloud Pub/Sub; `pubsub` waits for authenticated pushes
    # and renews the Gmail watch nightly. Polling is the default because it
    # works with nothing but the OAuth client.
    gmail_sync_mode: Literal["pubsub", "polling"] = "polling"
    gmail_poll_interval_minutes: int = Field(default=5, ge=1, le=60)
```

Directly after the `reject_non_tcp_redis_url` validator add:

```python
    @field_validator("gmail_poll_interval_minutes", mode="after")
    @classmethod
    def poll_interval_divides_the_hour(cls, value: int) -> int:
        """The poll cron runs at minutes `range(0, 60, interval)`. An interval
        that does not divide 60 leaves a short gap at every hour (7 polls at
        :49 and :56, then :00), so it is refused at startup instead."""
        if 60 % value:
            raise ValueError(
                "GMAIL_POLL_INTERVAL_MINUTES must divide 60: "
                "1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 or 60"
            )
        return value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_config.py -v`
Expected: all PASS. Values 0, -5, 61 and 120 are rejected by `ge`/`le`; 7 and 45 by the validator.

- [ ] **Step 5: Pass the settings to both containers**

In `docker-compose.yml`, in **both** Phase 9 blocks (service `api` and service `worker`), add after the `GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT` line:

```yaml
      GMAIL_SYNC_MODE: ${GMAIL_SYNC_MODE:-polling}
      GMAIL_POLL_INTERVAL_MINUTES: ${GMAIL_POLL_INTERVAL_MINUTES:-5}
```

In `.env.example`, replace the line `# Pub/Sub topic Gmail publishes to: projects/<project>/topics/<topic>` and everything after it up to (not including) the `# ---- Web` header with:

```sh
# How new mail is noticed. "polling" (default) checks every connected mailbox
# every GMAIL_POLL_INTERVAL_MINUTES (must divide 60) and needs nothing below.
# "pubsub" needs the three Pub/Sub values and a public HTTPS webhook. A
# deployment that already runs Pub/Sub must set GMAIL_SYNC_MODE=pubsub.
# GMAIL_SYNC_MODE=polling
# GMAIL_POLL_INTERVAL_MINUTES=5
# --- pubsub mode only ---
# Pub/Sub topic Gmail publishes to: projects/<project>/topics/<topic>
# GMAIL_PUBSUB_TOPIC=
# The push subscription's OIDC audience and service account. Both are checked
# on every push; with either unset the webhook answers 503.
# GMAIL_PUBSUB_PUSH_AUDIENCE=https://api.example.com/api/v1/gmail/pubsub
# GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT=gmail-push@<project>.iam.gserviceaccount.com

```

- [ ] **Step 6: Lint and commit**

```bash
cd apps/api && uv run ruff format app/core/config.py tests/unit/test_config.py && uv run ruff check app/core/config.py tests/unit/test_config.py && uv run mypy app/core/config.py
cd ../.. && git add apps/api/app/core/config.py apps/api/tests/unit/test_config.py docker-compose.yml .env.example
git commit -F - <<'EOF'
feat: add GMAIL_SYNC_MODE with polling as the default

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: Connect Gmail without Pub/Sub in polling mode

**Files:**
- Modify: `apps/api/app/api/gmail.py` (`_require_oauth_settings` ~line 92; `gmail_oauth_callback` ~lines 245–375; new helper `_start_mailbox_cursor`)
- Test: `apps/api/tests/unit/test_gmail_routes.py`

**Interfaces:**
- Consumes: `Settings.gmail_sync_mode` (Task 1).
- Produces: `_start_mailbox_cursor(gmail: GmailClient, access_token: str, profile: GmailProfile) -> tuple[str, datetime | None]` (private to `app/api/gmail.py`).

- [ ] **Step 1: Pin the existing callback tests to Pub/Sub**

With polling as the default, the two existing callback tests would stop calling `watch()`. In `apps/api/tests/unit/test_gmail_routes.py`, inside `test_valid_callback_saves_only_encrypted_refresh_token` and `test_reconnecting_a_disconnected_mailbox_starts_from_the_new_watch`, add this line to their `Settings(...)` call directly after `gmail_pubsub_topic="projects/test/topics/gmail",`:

```python
        gmail_sync_mode="pubsub",
```

- [ ] **Step 2: Write the failing tests**

Add to the imports at the top of `apps/api/tests/unit/test_gmail_routes.py`:

```python
from dataclasses import dataclass
```

Append to the end of the file:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_gmail_routes.py -v`
Expected: the two pinned tests and `test_a_failed_pubsub_connect_stops_the_watch_it_started` PASS. These FAIL:
- `test_oauth_start_needs_a_topic_only_in_pubsub_mode[polling-None-200]`: 422, because `_require_oauth_settings` still demands a topic.
- `test_polling_connect_...[None]`, `test_polling_reconnect_...`: `KeyError: 'location'`, because the callback answers that same 422 instead of redirecting.
- `test_polling_connect_...[projects/test/topics/gmail]`: `watch_calls == ['projects/test/topics/gmail']`.
- `test_a_failed_polling_connect_never_stops_a_watch`: `response` is the 422, not None.

- [ ] **Step 4: Implement**

In `apps/api/app/api/gmail.py`:

1. Change `from app.gmail.client import GmailClient` to:

```python
from app.gmail.client import GmailClient, GmailProfile
```

2. In `_require_oauth_settings`, replace

```python
    if not settings.gmail_pubsub_topic:
        raise ValidationError("Gmail Pub/Sub topic is not configured")
```

with

```python
    if settings.gmail_sync_mode == "pubsub" and not settings.gmail_pubsub_topic:
        raise ValidationError("Gmail Pub/Sub topic is not configured")
```

3. Add this helper directly above `@router.get("/connection", ...)`:

```python
async def _start_mailbox_cursor(
    gmail: GmailClient, access_token: str, profile: GmailProfile
) -> tuple[str, datetime | None]:
    """A new connection's history cursor and watch expiry, per sync mode.

    `pubsub` starts a watch so Gmail publishes changes, and the cursor is the
    watch's. `polling` starts nothing: the profile fetched a moment ago holds
    the mailbox's current `historyId`, and a topic still configured from
    Pub/Sub days is ignored.
    """
    settings = get_settings()
    if settings.gmail_sync_mode == "polling":
        return profile.history_id, None
    if not settings.gmail_pubsub_topic:
        raise ValidationError("Gmail Pub/Sub topic is not configured")
    watch = await gmail.watch(access_token, settings.gmail_pubsub_topic)
    return watch.history_id, watch.expiration
```

4. In `gmail_oauth_callback`, replace

```python
    settings = get_settings()
    if not settings.gmail_pubsub_topic:
        return _web_redirect("error", "gmail_notifications_not_configured")
```

with

```python
    settings = get_settings()
    if settings.gmail_sync_mode == "pubsub" and not settings.gmail_pubsub_topic:
        return _web_redirect("error", "gmail_notifications_not_configured")
```

5. Replace

```python
    gmail = GmailClient()
    try:
        watch = await gmail.watch(tokens.access_token, settings.gmail_pubsub_topic)
    except RuntimeError:
        return _web_redirect("error", "gmail_notifications_unavailable")
```

with

```python
    gmail = GmailClient()
    try:
        start_history_id, watch_expires_at = await _start_mailbox_cursor(
            gmail, tokens.access_token, profile
        )
    except RuntimeError:
        return _web_redirect("error", "gmail_notifications_unavailable")
```

6. In the `try: async with tenant_session(tenant) as session:` block that follows, make these replacements:
   - in the new-connection branch: `history_id=watch.history_id,` → `history_id=start_history_id,` and `watch_expires_at=watch.expiration,` → `watch_expires_at=watch_expires_at,`
   - in the existing-connection branch: `connection.history_id = watch.history_id` → `connection.history_id = start_history_id` and `connection.watch_expires_at = watch.expiration` → `connection.watch_expires_at = watch_expires_at`

7. Replace that block's `except Exception:` handler

```python
    except Exception:
        try:
            await gmail.stop_watch(tokens.access_token)
        except RuntimeError:
            pass
        raise
```

with

```python
    except Exception:
        # Only a watch this callback started is stopped. Polling mode starts
        # none, and a watch on this mailbox from Pub/Sub days is not ours.
        if watch_expires_at is not None:
            try:
                await gmail.stop_watch(tokens.access_token)
            except RuntimeError:
                pass
        raise
```

Leave the later old-mailbox `stop_watch` (when `existing_email != mailbox_email`) unchanged. It runs in both modes, because the previous mailbox may still hold a Pub/Sub-era watch.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_gmail_routes.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd apps/api && uv run ruff format app/api/gmail.py tests/unit/test_gmail_routes.py && uv run ruff check app/api/gmail.py tests/unit/test_gmail_routes.py && uv run mypy app/api/gmail.py
cd ../.. && git add apps/api/app/api/gmail.py apps/api/tests/unit/test_gmail_routes.py
git commit -F - <<'EOF'
feat: connect Gmail without Pub/Sub when polling

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Let a poll skip a mailbox another job is syncing

**Files:**
- Modify: `apps/api/app/gmail/sync.py` (`run_gmail_sync`, lines ~250–299)
- Test: `apps/api/tests/integration/test_gmail_pipeline.py`

**Interfaces:**
- Produces: `run_gmail_sync(*, mailbox_email: str, notification_history_id: str | None, force_sync: bool = False, skip_if_busy: bool = False) -> None`.

- [ ] **Step 1: Write the failing test**

In `apps/api/tests/integration/test_gmail_pipeline.py`, directly after `test_a_busy_mailbox_is_retried_instead_of_dropped`, add:

```python
async def test_a_busy_mailbox_is_left_to_the_next_poll(
    owner_connection: Any,
    tenant_a: TenantContext,
    fake_gmail: FakeGmail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gmail.sync import run_gmail_sync

    await seed_mailbox(owner_connection, tenant_a)
    fake_gmail.history_message_ids = ["m-busy"]
    monkeypatch.setattr("app.gmail.sync.get_redis", lambda: _Redis(held=True))

    # No Retry: the job holding the lock is syncing this mailbox right now,
    # and the next poll covers anything it listed too early to see.
    await run_gmail_sync(
        mailbox_email=_MAILBOX,
        notification_history_id=None,
        force_sync=True,
        skip_if_busy=True,
    )

    assert "m-busy" not in await message_rows(tenant_a)
    assert (await connection_row(tenant_a)).history_id == "100"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/integration/test_gmail_pipeline.py -v -k busy`
Expected: the new test FAILS with `TypeError: run_gmail_sync() got an unexpected keyword argument 'skip_if_busy'`. `test_a_busy_mailbox_is_retried_instead_of_dropped` PASSES.

- [ ] **Step 3: Implement**

In `apps/api/app/gmail/sync.py`, change the signature of `run_gmail_sync` to:

```python
async def run_gmail_sync(
    *,
    mailbox_email: str,
    notification_history_id: str | None,
    force_sync: bool = False,
    skip_if_busy: bool = False,
) -> None:
```

Replace the busy branch inside its loop

```python
        if not await lock.acquire(blocking=False):
            # Another job holds this mailbox and may have listed its history
            # before the change this notification announces, so skipping
            # would strand that mail until the next notification. Retry.
            logger.info("gmail_sync_busy_retrying", connection_id=str(connection_id))
            busy = True
            continue
```

with

```python
        if not await lock.acquire(blocking=False):
            if skip_if_busy:
                # A poll: the job holding the lock is syncing this mailbox
                # now, and the next poll picks up whatever it listed too early.
                logger.info("gmail_sync_busy_skipped", connection_id=str(connection_id))
                continue
            # Another job holds this mailbox and may have listed its history
            # before the change this notification announces, so skipping
            # would strand that mail until the next notification. Retry.
            logger.info("gmail_sync_busy_retrying", connection_id=str(connection_id))
            busy = True
            continue
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/integration/test_gmail_pipeline.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
cd apps/api && uv run ruff format app/gmail/sync.py tests/integration/test_gmail_pipeline.py && uv run ruff check app/gmail/sync.py tests/integration/test_gmail_pipeline.py && uv run mypy app/gmail/sync.py
cd ../.. && git add apps/api/app/gmail/sync.py apps/api/tests/integration/test_gmail_pipeline.py
git commit -F - <<'EOF'
feat: let a Gmail poll skip a mailbox that is already syncing

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Poll fan-out (list mailboxes, queue one poll each)

**Files:**
- Create: `apps/api/app/gmail/polling.py`
- Test: `apps/api/tests/unit/test_gmail_polling.py` (create)
- Test: `apps/api/tests/integration/test_gmail_polling.py` (create)

**Interfaces:**
- Produces (all in `app.gmail.polling`):
  - `gmail_poll_job_id(mailbox_email: str) -> str`
  - `async list_polled_mailboxes() -> list[str]`: case-folded, de-duplicated, in resolver order
  - `PollFanOut(enqueued: int, already_pending: int)`: frozen dataclass
  - `async enqueue_mailbox_polls(redis: ArqRedis, mailbox_emails: list[str], *, job_name: str, queue_name: str | None = None) -> PollFanOut`

- [ ] **Step 1: Write the failing unit tests**

Create `apps/api/tests/unit/test_gmail_polling.py`:

```python
from __future__ import annotations

import pytest

from app.gmail.polling import PollFanOut, enqueue_mailbox_polls, gmail_poll_job_id

pytestmark = pytest.mark.anyio


class _FakeArq:
    """arq's enqueue contract: None when the job ID already exists."""

    def __init__(self) -> None:
        self.job_ids: set[str] = set()
        self.calls: list[tuple[str, str, str | None, dict[str, object]]] = []

    async def enqueue_job(
        self, function: str, *, _job_id: str, _queue_name: str | None = None, **kwargs: object
    ) -> object | None:
        self.calls.append((function, _job_id, _queue_name, kwargs))
        if _job_id in self.job_ids:
            return None
        self.job_ids.add(_job_id)
        return object()


def test_poll_job_id_is_one_per_mailbox_whatever_the_spelling() -> None:
    assert gmail_poll_job_id(" Buyer@Example.com ") == gmail_poll_job_id("buyer@example.com")
    assert gmail_poll_job_id("buyer@example.com") != gmail_poll_job_id("other@example.com")


def test_poll_job_id_keeps_the_address_out_of_redis_keys() -> None:
    job_id = gmail_poll_job_id("buyer@example.com")

    assert job_id.startswith("gmail-poll:")
    assert len(job_id) == len("gmail-poll:") + 40
    assert "buyer" not in job_id
    assert "example" not in job_id


async def test_fan_out_queues_new_polls_and_counts_pending_ones() -> None:
    redis = _FakeArq()
    redis.job_ids.add(gmail_poll_job_id("busy@example.com"))

    result = await enqueue_mailbox_polls(
        redis,  # type: ignore[arg-type]
        ["new@example.com", "busy@example.com"],
        job_name="poll_gmail_mailbox_task",
    )

    assert result == PollFanOut(enqueued=1, already_pending=1)
    assert redis.calls[0] == (
        "poll_gmail_mailbox_task",
        gmail_poll_job_id("new@example.com"),
        None,
        {"mailbox_email": "new@example.com"},
    )
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_gmail_polling.py -v`
Expected: collection ERROR, `ModuleNotFoundError: No module named 'app.gmail.polling'`.

- [ ] **Step 3: Implement the module**

Create `apps/api/app/gmail/polling.py`:

```python
"""Polling mode's trigger: find the connected mailboxes, queue one sync each.

The sync itself is `run_gmail_sync`, unchanged. Polling replaces only what
starts it (docs/PHASE-9.md §2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from arq.connections import ArqRedis
from sqlalchemy import text

from app.core.tenancy import untenanted_session


@dataclass(frozen=True, slots=True)
class PollFanOut:
    enqueued: int
    already_pending: int


def gmail_poll_job_id(mailbox_email: str) -> str:
    """One fixed job ID per mailbox.

    With the poll task registered `keep_result=0`, arq refuses a second
    enqueue while this mailbox's poll is queued, running or waiting to retry,
    and accepts the next one once it ends (arq 0.28 `Worker.finish_job`
    deletes the job key when no result is kept). Hashed like
    `gmail_sync_job_id`, so the address is not a Redis key in clear.
    """
    normalized = mailbox_email.strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]
    return f"gmail-poll:{digest}"


async def list_polled_mailboxes() -> list[str]:
    """Every connected mailbox address, once.

    Read through the SECURITY DEFINER resolver the maintenance job uses: the
    worker has no tenant yet, and RLS shows an untenanted session no rows.
    It returns `connected` rows only, so a `needs_reauth` mailbox is not
    sent to Google with a dead refresh token every interval. One address
    connected by several organizations is polled once; `run_gmail_sync`
    already syncs every organization connected to it.
    """
    async with untenanted_session() as session:
        result = await session.execute(
            text("SELECT email FROM list_gmail_connections_for_maintenance()")
        )
        emails = [str(row[0]).strip().casefold() for row in result.all()]
    return list(dict.fromkeys(emails))


async def enqueue_mailbox_polls(
    redis: ArqRedis,
    mailbox_emails: list[str],
    *,
    job_name: str,
    queue_name: str | None = None,
) -> PollFanOut:
    """Queue `job_name` once per mailbox, skipping mailboxes still pending.

    `job_name` is passed in, not imported: the task lives in
    `app.workers.tasks`, which imports this module.
    """
    enqueued = 0
    for email in mailbox_emails:
        job = await redis.enqueue_job(
            job_name,
            mailbox_email=email,
            _job_id=gmail_poll_job_id(email),
            _queue_name=queue_name,
        )
        if job is not None:
            enqueued += 1
    return PollFanOut(enqueued=enqueued, already_pending=len(mailbox_emails) - enqueued)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_gmail_polling.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the integration tests**

Create `apps/api/tests/integration/test_gmail_polling.py`:

```python
"""Polling's fan-out against the real database and Redis.

Rows go into the shared dev database; `tenant_a`/`tenant_b` teardown deletes
their organizations, which cascades to these `gmail_connections` rows.
Redis jobs go to a queue no real worker reads, so a running dev worker
cannot consume one between the two enqueues a test compares.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import job_key_prefix
from sqlalchemy import text

from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.gmail.polling import (
    PollFanOut,
    enqueue_mailbox_polls,
    gmail_poll_job_id,
    list_polled_mailboxes,
)

pytestmark = pytest.mark.anyio

_JOB = "poll_gmail_mailbox_task"


def _address() -> str:
    return f"poll-{uuid.uuid4().hex[:12]}@example.com"


async def _connect(owner_connection: Any, tenant: TenantContext, email: str, status: str) -> None:
    await owner_connection.execute(
        text(
            "INSERT INTO gmail_connections "
            "(id, organization_id, email, refresh_token_ciphertext, history_id, status) "
            "VALUES (:id, :org, :email, :token, '100', :status)"
        ),
        {
            "id": uuid7(),
            "org": tenant.organization_id,
            "email": email,
            # ck_gmail_connections_connected_has_credential: only a
            # disconnected row may lack a credential.
            "token": None if status == "disconnected" else "ciphertext",
            "status": status,
        },
    )
    await owner_connection.commit()


@pytest.fixture
async def arq_pool() -> AsyncIterator[ArqRedis]:
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        yield pool
    finally:
        await pool.aclose()


@pytest.fixture
def queue() -> str:
    return f"test-gmail-poll-{uuid.uuid4().hex}"


async def _forget(pool: ArqRedis, email: str, queue: str) -> None:
    job_id = gmail_poll_job_id(email)
    await pool.zrem(queue, job_id)
    await pool.delete(job_key_prefix + job_id)


async def test_a_mailbox_connected_by_two_organizations_is_polled_once(
    owner_connection: Any, tenant_a: TenantContext, tenant_b: TenantContext
) -> None:
    email = _address()
    await _connect(owner_connection, tenant_a, email, "connected")
    await _connect(owner_connection, tenant_b, email, "connected")

    polled = await list_polled_mailboxes()

    assert polled.count(email) == 1


@pytest.mark.parametrize("status", ["needs_reauth", "disconnected"])
async def test_a_mailbox_that_cannot_sync_is_not_polled(
    owner_connection: Any, tenant_a: TenantContext, status: str
) -> None:
    email = _address()
    await _connect(owner_connection, tenant_a, email, status)

    assert email not in await list_polled_mailboxes()


async def test_a_mailbox_with_a_poll_pending_is_not_queued_again(
    arq_pool: ArqRedis, queue: str
) -> None:
    email = _address()
    try:
        first = await enqueue_mailbox_polls(arq_pool, [email], job_name=_JOB, queue_name=queue)
        second = await enqueue_mailbox_polls(arq_pool, [email], job_name=_JOB, queue_name=queue)
    finally:
        await _forget(arq_pool, email, queue)

    assert first == PollFanOut(enqueued=1, already_pending=0)
    assert second == PollFanOut(enqueued=0, already_pending=1)
```

- [ ] **Step 6: Run the integration tests**

Before the first integration run, tell the user it writes rows into the dev database `saas_ai`.

Run: `cd apps/api && $HOST_ENV uv run pytest tests/integration/test_gmail_polling.py -v`
Expected: all PASS. If they fail with `ConnectionError ... redis:6379`, `$HOST_ENV` was not applied.

- [ ] **Step 7: Lint and commit**

```bash
cd apps/api && uv run ruff format app/gmail/polling.py tests/unit/test_gmail_polling.py tests/integration/test_gmail_polling.py && uv run ruff check app/gmail/polling.py tests/unit/test_gmail_polling.py tests/integration/test_gmail_polling.py && uv run mypy app/gmail/polling.py
cd ../.. && git add apps/api/app/gmail/polling.py apps/api/tests/unit/test_gmail_polling.py apps/api/tests/integration/test_gmail_polling.py
git commit -F - <<'EOF'
feat: fan out one Gmail poll per connected mailbox

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Worker jobs and mode-dependent cron

**Files:**
- Modify: `apps/api/app/workers/tasks.py` (imports; new tasks after `sync_gmail_mailbox_task`)
- Modify: `apps/api/app/workers/settings.py` (imports; `build_cron_jobs`; `WorkerSettings.functions` / `cron_jobs`)
- Test: `apps/api/tests/unit/test_worker_settings.py` (replace `test_gmail_watch_renewal_is_scheduled_daily`; add tests)
- Test: `apps/api/tests/unit/test_gmail_polling.py` (add cron task tests)
- Test: `apps/api/tests/integration/test_gmail_polling.py` (add live worker test)
- Test: `apps/api/tests/integration/test_gmail_pipeline.py` (add end-to-end poll)

**Interfaces:**
- Consumes: `list_polled_mailboxes`, `enqueue_mailbox_polls`, `PollFanOut`, `gmail_poll_job_id` (Task 4); `run_gmail_sync(..., skip_if_busy=True)` (Task 3); `Settings.gmail_sync_mode`, `Settings.gmail_poll_interval_minutes` (Task 1).
- Produces:
  - `app.workers.tasks.poll_gmail_mailbox_task(ctx: dict[str, Any], *, mailbox_email: str) -> None`
  - `app.workers.tasks.poll_gmail_mailboxes_task(ctx: dict[str, Any]) -> None`
  - `app.workers.settings.GMAIL_POLL_MAX_TRIES = 3`
  - `app.workers.settings.build_cron_jobs(settings: Settings) -> list[CronJob]`

- [ ] **Step 1: Write the failing worker settings tests**

In `apps/api/tests/unit/test_worker_settings.py`, delete `test_gmail_watch_renewal_is_scheduled_daily` and add in its place:

```python
def _settings(**overrides: object):
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:password@localhost:5432/app",
        migration_database_url="postgresql+asyncpg://app_owner:password@localhost:5432/app",
        jwt_secret="unit-test-secret-that-is-long-enough",
        **overrides,
    )


def test_gmail_poll_task_keeps_no_result_so_the_next_interval_can_queue_it() -> None:
    """The poll's fixed job ID only frees up when arq deletes the job key,
    which it does at the end of a job only when no result is kept."""
    from app.workers.settings import GMAIL_POLL_MAX_TRIES
    from app.workers.tasks import poll_gmail_mailbox_task

    registered = {getattr(item, "coroutine", item): item for item in WorkerSettings.functions}
    function = registered[poll_gmail_mailbox_task]
    assert function.keep_result_s == 0
    assert function.max_tries == GMAIL_POLL_MAX_TRIES == 3


def test_polling_mode_schedules_the_poll_and_not_the_watch_renewal() -> None:
    from app.workers.settings import build_cron_jobs
    from app.workers.tasks import poll_gmail_mailboxes_task

    jobs = build_cron_jobs(_settings(gmail_sync_mode="polling", gmail_poll_interval_minutes=15))

    assert [job.coroutine for job in jobs] == [poll_gmail_mailboxes_task]
    assert jobs[0].minute == {0, 15, 30, 45}
    assert jobs[0].run_at_startup is True
    assert jobs[0].max_tries == 1
    assert jobs[0].timeout_s == 60


def test_pubsub_mode_renews_watches_daily_and_does_not_poll() -> None:
    from app.workers.settings import build_cron_jobs
    from app.workers.tasks import renew_gmail_watches_task

    jobs = build_cron_jobs(_settings(gmail_sync_mode="pubsub"))

    assert [job.coroutine for job in jobs] == [renew_gmail_watches_task]
    assert jobs[0].hour == 3
    assert jobs[0].minute == 0


def test_worker_cron_jobs_follow_the_configured_mode() -> None:
    from app.core.config import get_settings
    from app.workers.settings import build_cron_jobs

    expected = [job.coroutine for job in build_cron_jobs(get_settings())]
    assert [job.coroutine for job in WorkerSettings.cron_jobs] == expected
```

- [ ] **Step 2: Write the failing cron task tests**

Append to `apps/api/tests/unit/test_gmail_polling.py`:

```python
async def test_the_poll_cron_queues_the_registered_poll_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    async def mailboxes() -> list[str]:
        return ["a@example.com"]

    redis = _FakeArq()
    monkeypatch.setattr(tasks, "list_polled_mailboxes", mailboxes)

    await tasks.poll_gmail_mailboxes_task({"redis": redis})

    assert [(call[0], call[3]) for call in redis.calls] == [
        (tasks.poll_gmail_mailbox_task.__name__, {"mailbox_email": "a@example.com"})
    ]


async def test_a_poll_with_no_connected_mailboxes_queues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    async def no_mailboxes() -> list[str]:
        return []

    redis = _FakeArq()
    monkeypatch.setattr(tasks, "list_polled_mailboxes", no_mailboxes)

    await tasks.poll_gmail_mailboxes_task({"redis": redis})

    assert redis.calls == []


async def test_a_poll_syncs_from_the_cursor_and_skips_a_busy_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    calls: list[dict[str, object]] = []

    async def sync(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(tasks, "run_gmail_sync", sync)

    await tasks.poll_gmail_mailbox_task({}, mailbox_email="a@example.com")

    assert calls == [
        {
            "mailbox_email": "a@example.com",
            "notification_history_id": None,
            "force_sync": True,
            "skip_if_busy": True,
        }
    ]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_worker_settings.py tests/unit/test_gmail_polling.py -v`
Expected: the new tests FAIL with `ImportError` (`build_cron_jobs`, `GMAIL_POLL_MAX_TRIES`, `poll_gmail_mailbox_task`) or `AttributeError` on `tasks.poll_gmail_mailboxes_task`. The Task 4 tests still PASS.

- [ ] **Step 4: Add the tasks**

In `apps/api/app/workers/tasks.py`, add below the `from app.gmail.sync import (...)` block:

```python
from app.gmail.polling import enqueue_mailbox_polls, list_polled_mailboxes
```

Add directly after `sync_gmail_mailbox_task`:

```python
async def poll_gmail_mailbox_task(ctx: dict[str, Any], *, mailbox_email: str) -> None:
    """Polling mode: sync one mailbox from its saved cursor.

    A mailbox another job is syncing right now is skipped, not retried: the
    next poll covers it.
    """
    await run_gmail_sync(
        mailbox_email=mailbox_email,
        notification_history_id=None,
        force_sync=True,
        skip_if_busy=True,
    )


async def poll_gmail_mailboxes_task(ctx: dict[str, Any]) -> None:
    """Polling mode's cron: queue one `poll_gmail_mailbox_task` per connected
    mailbox, so one slow mailbox does not hold up the others."""
    mailboxes = await list_polled_mailboxes()
    fan_out = await enqueue_mailbox_polls(
        ctx["redis"], mailboxes, job_name=poll_gmail_mailbox_task.__name__
    )
    logger.info(
        "gmail_poll_started",
        mailboxes=len(mailboxes),
        enqueued=fan_out.enqueued,
        already_pending=fan_out.already_pending,
    )
```

- [ ] **Step 5: Register them**

In `apps/api/app/workers/settings.py`:

1. Keep the `collections.abc` and `typing` imports, and make the rest read:

```python
from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.worker import Function, func

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.workers.tasks import (
    EVALUATION_MAX_TRIES,
    import_products_task,
    ingest_document_task,
    poll_gmail_mailbox_task,
    renew_gmail_watches_task,
    reprocess_supplier_message_task,
    run_evaluation_task,
    sync_gmail_mailbox_task,
    title_conversation_task,
)
```

2. Below `GMAIL_JOB_MAX_TRIES = 20` add:

```python
#: A poll that keeps failing gives up quickly: the next interval polls again.
GMAIL_POLL_MAX_TRIES = 3


def build_cron_jobs(settings: Settings) -> list[CronJob]:
    """Cron jobs for the configured Gmail sync mode (docs/PHASE-9.md §2).

    `pubsub` renews watches at 03:00 and reconciles each mailbox. `polling`
    polls instead and must not renew: its connections have no watch, so
    renewal would treat every one as expired and mark it
    `gmail_topic_not_configured`.
    """
    if settings.gmail_sync_mode == "polling":
        return [
            cron(
                "app.workers.tasks.poll_gmail_mailboxes_task",
                minute=set(range(0, 60, settings.gmail_poll_interval_minutes)),
                # A worker that just started checks right away instead of
                # waiting for the next slot.
                run_at_startup=True,
                timeout=60,
                max_tries=1,
            ),
        ]
    return [
        cron(
            "app.workers.tasks.renew_gmail_watches_task",
            hour=3,
            minute=0,
            timeout=3600,
            max_tries=1,
        ),
    ]
```

3. In `WorkerSettings.functions`, directly after the `func(reprocess_supplier_message_task, ...)` line add:

```python
        # No result kept: arq then deletes the job key when a poll ends, and
        # that key is what stops a mailbox having two polls in flight
        # (`gmail_poll_job_id`).
        func(poll_gmail_mailbox_task, keep_result=0, max_tries=GMAIL_POLL_MAX_TRIES),
```

4. Replace the whole `cron_jobs = [ ... ]` assignment in `WorkerSettings` with:

```python
    cron_jobs = build_cron_jobs(get_settings())
```

- [ ] **Step 6: Run the unit tests to verify they pass**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/unit/test_worker_settings.py tests/unit/test_gmail_polling.py -v`
Expected: all PASS.

- [ ] **Step 7: Write the live worker test (Review Focus 4)**

Append to `apps/api/tests/integration/test_gmail_polling.py`:

```python
@pytest.mark.parametrize("outcome", ["succeeds", "fails"])
async def test_a_finished_poll_frees_its_mailbox_for_the_next_interval(
    arq_pool: ArqRedis, queue: str, outcome: str
) -> None:
    """A real arq worker runs a poll registered exactly as production
    registers it. If the job key outlived the job, the fixed job ID would
    block that mailbox's polls forever, with nothing anywhere saying so."""
    from arq.worker import Worker, func

    from app.workers.settings import WorkerSettings
    from app.workers.tasks import poll_gmail_mailbox_task

    registered = next(
        item
        for item in WorkerSettings.functions
        if getattr(item, "coroutine", None) is poll_gmail_mailbox_task
    )
    email = _address()
    ran: list[str] = []

    async def stand_in(_ctx: dict[str, Any], *, mailbox_email: str) -> None:
        ran.append(mailbox_email)
        if outcome == "fails":
            raise RuntimeError("Gmail is down")

    worker = Worker(
        functions=[
            func(
                stand_in,
                name=registered.name,
                keep_result=registered.keep_result_s,
                max_tries=registered.max_tries,
            )
        ],
        queue_name=queue,
        redis_pool=arq_pool,
        burst=True,
        poll_delay=0.01,
        handle_signals=False,
    )
    try:
        await enqueue_mailbox_polls(arq_pool, [email], job_name=registered.name, queue_name=queue)
        await worker.main()
        again = await enqueue_mailbox_polls(
            arq_pool, [email], job_name=registered.name, queue_name=queue
        )
    finally:
        await _forget(arq_pool, email, queue)

    assert ran == [email]
    assert again == PollFanOut(enqueued=1, already_pending=0)
```

`handle_signals=False` is required on Windows, where the event loop has no `add_signal_handler`. Don't call `worker.close()`: it would close `arq_pool`, which the fixture owns.

- [ ] **Step 8: Write the end-to-end poll test**

In `apps/api/tests/integration/test_gmail_pipeline.py`, after `test_a_busy_mailbox_is_left_to_the_next_poll` (Task 3), add:

```python
async def test_a_poll_processes_new_mail_from_the_saved_cursor(
    owner_connection: Any, tenant_a: TenantContext, fake_gmail: FakeGmail
) -> None:
    from app.workers.tasks import poll_gmail_mailbox_task

    await seed_mailbox(owner_connection, tenant_a)
    fake_gmail.messages["m-poll"] = gmail_message("m-poll", attachment_id="att-poll")
    fake_gmail.attachments["att-poll"] = workbook_bytes([("SKU-1", "Widget", 10)])
    fake_gmail.history_message_ids = ["m-poll"]

    await poll_gmail_mailbox_task({}, mailbox_email=_MAILBOX)

    rows = await message_rows(tenant_a)
    assert rows["m-poll"].status == "processed"
    assert await report_for(tenant_a, rows["m-poll"]) is not None
    # No notification carried a history ID; the cursor still advances to
    # the newest one history.list reported.
    assert (await connection_row(tenant_a)).history_id == "200"
```

- [ ] **Step 9: Run the integration tests**

Run: `cd apps/api && $HOST_ENV uv run pytest tests/integration/test_gmail_polling.py tests/integration/test_gmail_pipeline.py -v`
Expected: all PASS, including both `outcome` cases.

- [ ] **Step 10: Lint and commit**

```bash
cd apps/api && uv run ruff format app/workers tests/unit/test_worker_settings.py tests/unit/test_gmail_polling.py tests/integration/test_gmail_polling.py tests/integration/test_gmail_pipeline.py && uv run ruff check app/workers tests/unit/test_worker_settings.py tests/unit/test_gmail_polling.py tests/integration/test_gmail_polling.py tests/integration/test_gmail_pipeline.py && uv run mypy app/workers
cd ../.. && git add apps/api/app/workers apps/api/tests/unit/test_worker_settings.py apps/api/tests/unit/test_gmail_polling.py apps/api/tests/integration/test_gmail_polling.py apps/api/tests/integration/test_gmail_pipeline.py
git commit -F - <<'EOF'
feat: poll connected Gmail mailboxes on a cron in polling mode

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Documentation and full verification

**Files:**
- Modify: `docs/PHASE-9.md`
- Modify: `README.md` (Phase 9 row, line ~349)

- [ ] **Step 1: Update the overview diagram in `docs/PHASE-9.md`**

Replace the fenced `text` diagram at the top with:

```text
polling (default): arq cron every GMAIL_POLL_INTERVAL_MINUTES ─┐  one job per connected mailbox
pubsub:  Gmail (watch) ──▶ Pub/Sub ──authenticated push──▶ POST /api/v1/gmail/pubsub
                                                           │ verify OIDC token,
                                                           │ resolve mailbox → tenant
                                                           ▼
                                         arq: poll_gmail_mailbox_task / sync_gmail_mailbox_task
                                           history pages since the saved cursor
                                           → new inbox messages from known suppliers
                                           → fetch .xlsx attachments (in memory)
                                           → mapping: confirmed? parse : propose + review
                                           → match catalogue, rank, persist report
                                                           │
                                                           ▼
                                 /dashboard/supplier-quotes (GraphQL)
```

- [ ] **Step 2: Update §1 Scope**

In §1, replace the bullet that begins `- Pub/Sub push webhook.` with these two bullets:

```markdown
- **Polling (default).** Every `GMAIL_POLL_INTERVAL_MINUTES` (5 by default) an arq cron
  queues one sync per connected mailbox, with at most one in flight per mailbox. It needs
  no Google Cloud Pub/Sub and no public URL.
- **Pub/Sub push (`GMAIL_SYNC_MODE=pubsub`).** Every push is verified (signature, issuer,
  audience, service account and verified email) before any database lookup happens.
```

Replace the bullet `- Daily watch renewal and cursor reconciliation (\`renew_gmail_watches_task\`, 03:00 UTC).` with:

```markdown
- Pub/Sub mode only: daily watch renewal and cursor reconciliation
  (`renew_gmail_watches_task`, 03:00 UTC). Polling mode does not schedule it.
```

- [ ] **Step 3: Update §2 Configuration**

Replace the sentence `Every setting is blank by default, and the feature stays off until all of them are set.` with:

```markdown
The OAuth and encryption settings are blank by default, and the feature stays off until
they are set. The three Pub/Sub settings are needed only when `GMAIL_SYNC_MODE=pubsub`.
```

In the table, after the `GMAIL_TOKEN_ENCRYPTION_KEY` row, add:

```markdown
| `GMAIL_SYNC_MODE` | `polling` (default) or `pubsub`. See §2.1. |
| `GMAIL_POLL_INTERVAL_MINUTES` | Polling mode: minutes between checks, default `5`. Must divide 60. |
```

and change the first cell of the three Pub/Sub rows to read, respectively, `` `GMAIL_PUBSUB_TOPIC` (pubsub only) ``, `` `GMAIL_PUBSUB_PUSH_AUDIENCE` (pubsub only) `` and `` `GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT` (pubsub only) ``.

After the `Tunables with safe defaults:` paragraph, add:

```markdown
### 2.1 Choosing a sync mode

- **`polling`** (default). Each connected mailbox's history is read every
  `GMAIL_POLL_INTERVAL_MINUTES`. New mail reaches the dashboard within one interval plus
  processing time. Connecting takes the starting cursor from the Gmail profile and creates
  no watch. A mailbox in `needs_reauth` is not polled until its owner reconnects. A mailbox
  that another job is still syncing is skipped for that interval.
- **`pubsub`**. Gmail pushes a notification for each change, so mail arrives within
  seconds. It needs the Google Cloud setup in §3 and a public HTTPS webhook.

**Upgrading:** polling became the default on 2026-10-09. A deployment that already runs
Pub/Sub must set `GMAIL_SYNC_MODE=pubsub` to keep it.

**Switching modes:**

- `pubsub` → `polling`: existing watches expire on their own within 7 days. Pushes that
  arrive until then queue the same idempotent sync and do no harm.
- `polling` → `pubsub`: connections have no watch until the next 03:00 UTC renewal, so no
  push arrives for up to a day. Reconnect Gmail to start a watch immediately.
```

- [ ] **Step 4: Update §3 and §7**

At the start of §3, directly under the heading, add:

```markdown
Only for `GMAIL_SYNC_MODE=pubsub`. In polling mode, do steps 1 (Gmail API only), 6, 7
and 8, and skip the Pub/Sub topic and subscription.
```

In §7, replace the paragraph that begins `A real end-to-end run needs a real Google project (§3)` with:

```markdown
A real end-to-end run needs a Google OAuth client (§3, steps 6–8). In polling mode that is
all: run the API and the worker locally, connect Gmail, and new mail is picked up within
one interval. Pub/Sub mode also needs a public HTTPS URL for the push endpoint. Pub/Sub
cannot push to `localhost`, so use a tunnel or a deployed API.

Polling is covered by `test_gmail_polling.py` (unit: job IDs and the cron task;
integration: which mailboxes are polled, at most one poll in flight per mailbox, and a
real arq worker freeing the job ID after a poll ends) and by the poll cases in
`test_gmail_pipeline.py`.
```

- [ ] **Step 5: Update the README row**

In `README.md`, in the Phase 9 table row, replace `authenticated Pub/Sub push; idempotent mailbox sync from the watch cursor;` with:

```markdown
mailbox polling every 5 minutes by default, or authenticated Pub/Sub push; idempotent mailbox sync from the history cursor;
```

- [ ] **Step 6: Run the full verification**

Run from the repo root:

```bash
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app/
cd apps/api && $HOST_ENV uv run pytest -q
```

Expected: ruff and mypy clean, and the whole suite passing. If only `test_rls.py`/`test_isolation_layers.py` fail, check for leftover `org-a`/`org-b` slugs before suspecting this change.

- [ ] **Step 7: Commit**

```bash
git add docs/PHASE-9.md README.md
git commit -F - <<'EOF'
docs: document Gmail polling mode and switching sync modes

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
