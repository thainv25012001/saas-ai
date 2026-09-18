import asyncio
import json
import unittest.mock
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agents.service import AgentService
from app.api import chat as chat_api
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.db.session import engine
from app.llm.base import ModelCapabilities
from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    Usage,
)
from app.main import create_app
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

CHAT_URL = "/api/v1/chat/stream"


@pytest.fixture
def app():
    """A fresh app instance per test, so `dependency_overrides` never leaks
    between tests (each test gets its own FastAPI app object)."""
    return create_app()


@pytest.fixture
async def api_client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _parse_events(body: str) -> list[dict[str, object]]:
    """Pull every `data: {...}` line out of an SSE body, in order.

    Deliberately ignores `: ping` comment lines and blank separators -- those
    are asserted on separately (as raw text) by the heartbeat test.
    """
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


async def _register(api_client: AsyncClient, email: str, org_name: str = "Ada Motors Chat") -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Chat Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


async def _make_agent(org_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    name = str(overrides.pop("name", "Sales Bot"))
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name, **overrides))
    return agent.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _NoNewConversation:
    """Stands in for `ChatService` in the tests below that drive
    `_stream_body` directly.

    `created_conversation = None` means the turn continued an existing
    conversation, so no title is queued -- which keeps those tests about the
    one thing they are each testing (session teardown, heartbeats, pump
    failures) rather than about titling.
    """

    created_conversation = None


class _SlowProvider:
    """Mimics FakeProvider but sleeps between chunks -- a real (tiny) delay,
    so the heartbeat's `asyncio.wait_for` timeout actually has a chance to
    fire. Used only by the heartbeat test, with HEARTBEAT_INTERVAL_SECONDS
    patched down to a few milliseconds -- no test sleeps for the real 15s
    interval.
    """

    name = "fake"

    def __init__(self, chunks: list[str], delay: float) -> None:
        self._chunks = chunks
        self._delay = delay

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            content=[TextBlock(text="".join(self._chunks))],
            usage=Usage(input_tokens=1, output_tokens=1),
            model=request.model,
            stop_reason="end_turn",
        )

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        yield MessageStartEvent(model=request.model)
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            yield TextDeltaEvent(text=chunk)
        yield MessageEndEvent(
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
            model=request.model,
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate_structured(self, request, schema):  # type: ignore[no-untyped-def]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Unauthenticated
# ---------------------------------------------------------------------------


async def test_unauthenticated_returns_401_json_envelope(api_client, clean_users):
    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(uuid.uuid4()), "message": "hi"}
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# 2. Content type + streaming headers
# ---------------------------------------------------------------------------


async def test_response_has_streaming_headers(app, api_client, clean_users):
    token = await _register(api_client, "stream-headers@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert "content-encoding" not in response.headers


# ---------------------------------------------------------------------------
# 3. Event sequence / deltas reassemble
# ---------------------------------------------------------------------------


async def test_event_sequence_and_deltas_reassemble(app, api_client, clean_users):
    token = await _register(api_client, "sequence@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["Hel", "lo!"]
    )

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    events = _parse_events(response.text)
    assert [e["type"] for e in events] == [
        "message_start",
        "text_delta",
        "text_delta",
        "message_end",
    ]
    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == "Hello!"
    assert uuid.UUID(str(events[0]["conversation_id"]))
    assert uuid.UUID(str(events[0]["message_id"]))


# ---------------------------------------------------------------------------
# 4. message_end fields
# ---------------------------------------------------------------------------


async def test_message_end_carries_usage_cost_model_and_prompt_version(
    app, api_client, clean_users
):
    token = await _register(api_client, "message-end@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="claude-sonnet-5")
    usage = Usage(input_tokens=42, output_tokens=17)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["ok"], usage=usage
    )

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    events = _parse_events(response.text)
    end = next(e for e in events if e["type"] == "message_end")
    assert end["usage"] == {"input_tokens": 42, "output_tokens": 17}
    assert end["model"] == "claude-sonnet-5"
    assert float(str(end["cost_usd"])) > 0
    # No prompt was configured on the agent, so the fallback default was
    # used -- prompt_version_id must be None, not a fabricated id.
    assert end["prompt_version_id"] is None


# ---------------------------------------------------------------------------
# 5. Unknown agent_id
# ---------------------------------------------------------------------------


async def test_unknown_agent_id_returns_404_json_envelope(app, api_client, clean_users):
    token = await _register(api_client, "unknown-agent@example.com")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(uuid.uuid4()), "message": "hello"},
        headers=_auth(token),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # Must not be a 200 stream with an in-band error event.
    assert "text/event-stream" not in response.headers["content-type"]


async def test_pre_stream_error_does_not_leak_the_database_session(app, api_client, clean_users):
    """The route opens its `tenant_session` manually (`__aenter__`, not
    `async with`) because it has to outlive the coroutine -- the streaming
    body still needs it. A pre-stream failure (this unknown-agent case)
    returns before that streaming body ever exists, so the route itself is
    the only thing that can still release the connection; skipping that
    would leak one pooled connection per failed request. `engine.pool` is a
    process-wide singleton, so counting checked-out connections before and
    after is a direct check that the session was actually closed rather than
    abandoned on the failure path.
    """
    token = await _register(api_client, "pre-stream-leak@example.com")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    before = engine.pool.checkedout()
    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(uuid.uuid4()), "message": "hello"},
        headers=_auth(token),
    )
    assert response.status_code == 404
    after = engine.pool.checkedout()
    assert after == before


async def test_unresolvable_provider_returns_500_json_envelope_not_a_stream(
    api_client, clean_users
):
    """An agent configured for a provider this deployment cannot reach (no
    API key set) is exactly the "provider misconfiguration" case named in
    this module's docstring: `ChatService.send()` resolves the provider
    before creating a conversation specifically so this lands as a plain
    JSON error, not an in-band event. Deliberately does not override
    `get_chat_provider` -- the whole point is to exercise its real
    production default (`None`, letting `ChatService` call
    `app.llm.registry.get_provider` itself) against an agent whose
    `provider` column names "openai", with no `OPENAI_API_KEY` configured
    anywhere in this test environment.
    """
    token = await _register(api_client, "unresolvable-provider@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, provider="openai")

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "llm_misconfigured"
    assert "text/event-stream" not in response.headers["content-type"]


# ---------------------------------------------------------------------------
# 6. Cross-tenant agent_id
# ---------------------------------------------------------------------------


async def test_cross_tenant_agent_id_returns_404_json_not_a_stream(app, api_client, clean_users):
    owner_token = await _register(api_client, "org-a-owner@example.com", "Ada Motors Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _make_agent(owner_org_id)

    other_token = await _register(api_client, "org-b-owner@example.com", "Ada Motors Org B")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello"},
        headers=_auth(other_token),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "text/event-stream" not in response.headers["content-type"]


# ---------------------------------------------------------------------------
# 7. Mid-stream provider failure
# ---------------------------------------------------------------------------


async def test_mid_stream_provider_failure_emits_error_event_after_deltas(
    app, api_client, clean_users
):
    token = await _register(api_client, "mid-stream-failure@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    provider = FakeProvider(
        script=["partial ", "output that never completes"],
        fail_with=LLMUnavailableError("upstream dropped the connection"),
    )
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 200
    events = _parse_events(response.text)
    delta_index = next(i for i, e in enumerate(events) if e["type"] == "text_delta")
    error_index = next(i for i, e in enumerate(events) if e["type"] == "error")
    assert error_index > delta_index
    error_event = events[error_index]
    assert error_event["code"] == "llm_unavailable"
    assert "message" in error_event
    # Nothing after the error.
    assert error_index == len(events) - 1


async def test_unexpected_non_app_error_mid_stream_still_ends_in_an_error_event(
    app, api_client, clean_users
):
    """`ChatService.send()` only catches `AppError` around the provider's
    stream (see `app/chat/service.py`) -- anything else escaping it (a bug,
    or a domain error raised from a code path that try/except does not wrap)
    reaches the router as a raw exception, well after the 200 status line
    was already sent. This is the same "cannot un-send the status line"
    situation as a normalized provider failure, so it must degrade to the
    same shape: a terminal `error` event, not a truncated connection.

    Critically, the event must NOT carry the real exception's text. A raw
    `IntegrityError`'s `str()` contains the failing SQL, bound parameters
    (including the user's own message), and constraint/table names --
    `app/graphql/schema.py`'s `AppErrorExtension` replaces exactly this for
    the same reason on the GraphQL surface, and there is no generic
    `Exception` handler on REST to catch a leak here otherwise.
    """
    token = await _register(api_client, "unexpected-failure@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    provider = FakeProvider(
        script=["partial ", "output that never completes"],
        fail_with=RuntimeError("boom -- SELECT secret_column FROM tenants"),
    )
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    logged: list[dict[str, object]] = []
    real_error = chat_api.logger.error

    def _recording_error(event: str, **kwargs: object) -> None:
        logged.append({"event": event, **kwargs})
        real_error(event, **kwargs)

    with unittest.mock.patch.object(chat_api.logger, "error", side_effect=_recording_error):
        response = await api_client.post(
            CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
        )

    assert response.status_code == 200
    # The real exception text must not reach the client anywhere in the body.
    assert "boom" not in response.text
    assert "secret_column" not in response.text

    events = _parse_events(response.text)
    assert events[0]["type"] == "message_start"
    delta_index = next(i for i, e in enumerate(events) if e["type"] == "text_delta")
    error_index = next(i for i, e in enumerate(events) if e["type"] == "error")
    assert error_index > delta_index
    error_event = events[error_index]
    assert error_event["code"] == "internal_error"
    assert error_event["message"] == "internal server error"
    assert error_index == len(events) - 1

    # The real exception went to the server-side log instead.
    assert len(logged) == 1
    assert logged[0]["event"] == "chat_stream_unexpected_error"
    assert isinstance(logged[0]["exc_info"], RuntimeError)
    assert "boom" in str(logged[0]["exc_info"])


async def test_unexpected_non_app_error_rolls_back_the_whole_turn(
    app, api_client, clean_users, owner_connection
):
    """An exception `ChatService.send()` did not expect is often itself
    evidence the data is suspect (frequently a database error). Unlike a
    normalized provider `AppError` -- where the partial assistant reply is
    deliberately kept because the user already saw those tokens -- this path
    must roll the whole transaction back rather than commit a conversation
    and a user message with no assistant reply and no `usage_events` row:
    exactly the half-written shape `ChatService.send`'s own docstring says
    must never be left behind.
    """
    token = await _register(api_client, "rollback-on-bug@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    provider = FakeProvider(
        script=["partial ", "output"],
        fail_with=RuntimeError("boom"),
    )
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )
    assert response.status_code == 200

    count = await owner_connection.execute(
        text("SELECT count(*) FROM conversations WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    # Rolled back entirely -- not even the conversation or the user's own
    # message survive. Committing them (with no assistant reply alongside)
    # would be exactly the inconsistent half-write this path must avoid.
    assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# 8. Conversation continuation
# ---------------------------------------------------------------------------


async def test_second_request_with_conversation_id_continues_same_conversation(
    app, api_client, clean_users, owner_connection
):
    token = await _register(api_client, "continuation@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["first reply"]
    )

    first = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )
    first_events = _parse_events(first.text)
    conversation_id = first_events[0]["conversation_id"]

    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["second reply"]
    )
    second = await api_client.post(
        CHAT_URL,
        json={
            "agent_id": str(agent_id),
            "message": "and again",
            "conversation_id": conversation_id,
        },
        headers=_auth(token),
    )
    second_events = _parse_events(second.text)

    assert second_events[0]["conversation_id"] == conversation_id

    count = await owner_connection.execute(
        text("SELECT count(*) FROM conversations WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    assert count.scalar_one() == 1

    messages = await owner_connection.execute(
        text("SELECT role, seq FROM messages WHERE conversation_id = :cid ORDER BY seq"),
        {"cid": conversation_id},
    )
    rows = messages.all()
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert [r.role for r in rows] == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# Extra: heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat_ping_appears_while_stream_is_open(
    app, api_client, clean_users, monkeypatch
):
    monkeypatch.setattr(chat_api, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    token = await _register(api_client, "heartbeat@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: _SlowProvider(
        chunks=["a", "b", "c"], delay=0.08
    )

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 200
    assert ": ping" in response.text
    events = _parse_events(response.text)
    assert [e["type"] for e in events] == [
        "message_start",
        "text_delta",
        "text_delta",
        "text_delta",
        "message_end",
    ]


# ---------------------------------------------------------------------------
# Extra: abrupt close (client disconnect) rolls back, it does not commit
# ---------------------------------------------------------------------------


async def test_stream_body_rolls_back_the_session_on_abrupt_close():
    """A client disconnecting mid-stream (or the ASGI server tearing down the
    task for any other reason) throws `GeneratorExit` into `_stream_body`
    at whatever `await` it is currently suspended on -- well after its `try`
    block has started. That has to reach `session_cm.__aexit__` as a real
    exception so the transaction rolls back; passing `(None, None, None)`
    unconditionally in `finally` (as a simpler version of this generator
    once did) would instead commit a request that never finished, every
    single time a client goes away mid-stream.

    Exercised directly against `_stream_body` -- httpx's ASGI transport
    reads a response to completion and has no way to simulate a client
    hanging up partway through, which is the only way this path triggers.
    """
    from app.api.chat import ChatMessageStart, _stream_body

    class _RecordingSessionCM:
        def __init__(self) -> None:
            self.exit_args: tuple[object, object, object] | None = None

        async def __aenter__(self) -> "_RecordingSessionCM":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self.exit_args = (exc_type, exc, tb)
            return False

    async def _hangs_forever() -> AsyncIterator[object]:
        # Stands in for a provider still streaming when the client goes
        # away: never produces a second event, so `_stream_body` is left
        # suspended waiting for one -- exactly where a real disconnect would
        # interrupt it.
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable; keeps this an async generator

    session_cm = _RecordingSessionCM()
    first_event = ChatMessageStart(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    body = _stream_body(_hangs_forever(), first_event, session_cm, _NoNewConversation())  # type: ignore[arg-type]

    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"data: ")

    await body.aclose()

    assert session_cm.exit_args is not None
    exc_type, exc, _tb = session_cm.exit_args
    assert exc_type is GeneratorExit


async def test_session_still_closes_if_the_pump_task_raises_an_unexpected_baseexception():
    """`_pump` only catches `Exception` (deliberately -- it must never
    swallow the `CancelledError` this generator's own cleanup sends it via
    `pump_task.cancel()`). If `events` itself ever raised something that is
    a `BaseException` but not an `Exception` (a bug well outside the
    documented contract, but not one Python prevents), that exception
    surfaces from `await pump_task` in `_stream_body`'s `finally`. Closing
    the session has to happen regardless -- a single flat `finally` block
    where `await session_cm.__aexit__(...)` is the last statement would skip
    it entirely, leaking the exact connection the pre-stream-error leak test
    above guards against.
    """
    from app.api.chat import ChatMessageStart, _stream_body

    class _RecordingSessionCM:
        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self) -> "_RecordingSessionCM":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self.closed = True
            return False

    class _SimulatedBug(BaseException):
        """A `BaseException` that is not `Exception` -- and deliberately not
        `SystemExit`/`KeyboardInterrupt` either: `asyncio.Task` special-cases
        those two and re-raises them immediately out of the event loop
        rather than storing them as the task's result, which would crash
        this test outright instead of exercising `await pump_task`'s normal
        exception-propagation path."""

    async def _raises_unexpected_baseexception() -> AsyncIterator[object]:
        # `_pump`'s `except Exception` does not catch this, so it
        # terminates `pump_task` directly, uncaught.
        raise _SimulatedBug("simulated bug outside _pump's documented contract")
        yield  # pragma: no cover - unreachable; keeps this an async generator

    session_cm = _RecordingSessionCM()
    first_event = ChatMessageStart(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    body = _stream_body(
        _raises_unexpected_baseexception(), first_event, session_cm, _NoNewConversation()
    )  # type: ignore[arg-type]

    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"data: ")

    # Give the pump task a chance to actually run and crash before closing.
    await asyncio.sleep(0.05)

    with pytest.raises(_SimulatedBug):
        await body.aclose()

    assert session_cm.closed is True


async def test_disconnect_after_completed_turn_logs_discarded_usage(monkeypatch):
    """The pump drives `ChatService.send()` to completion, including its
    `record_usage` write for an LLM call the organization has already been
    billed for by the provider. If the client then disconnects before
    `_stream_body` reaches `_STREAM_DONE`, the abrupt-close path rolls that
    write back (see the test above) -- silently, from the organization's
    point of view, since nothing else in this request records that the call
    ever happened. This does not fix that (real fix needs usage accounting
    outside this transaction, see `docs/PHASE-2.md` §5) but pins that the
    loss is at least logged with enough detail (model, tokens, cost) to
    reconcile from logs later.
    """
    from decimal import Decimal

    from app.api.chat import ChatMessageEnd, ChatMessageStart, _stream_body

    class _NoOpSessionCM:
        async def __aenter__(self) -> "_NoOpSessionCM":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    async def _completes_then_hangs() -> AsyncIterator[object]:
        # Stands in for `ChatService.send()` having already flushed the
        # assistant message and the usage_events row for a finished LLM
        # call, with the client vanishing before the generator can reach
        # its own natural end.
        yield ChatMessageEnd(
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=Decimal("0.00123"),
            latency_ms=250,
            model="fake-1",
            prompt_version_id=None,
        )
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable; keeps this an async generator

    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        chat_api.logger,
        "warning",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )

    first_event = ChatMessageStart(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    body = _stream_body(
        _completes_then_hangs(), first_event, _NoOpSessionCM(), _NoNewConversation()
    )  # type: ignore[arg-type]

    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"data: ")
    second_chunk = await body.__anext__()
    assert b'"type": "message_end"' in second_chunk

    await body.aclose()

    assert len(logged) == 1
    entry = logged[0]
    assert entry["event"] == "chat_stream_discarded_completed_turn_usage"
    assert entry["model"] == "fake-1"
    assert entry["input_tokens"] == 10
    assert entry["output_tokens"] == 5
    assert entry["cost_usd"] == "0.00123"


# ---------------------------------------------------------------------------
# 9. The request body is bounded, and the endpoint is throttled
# ---------------------------------------------------------------------------


async def test_empty_message_is_rejected_before_anything_is_spent(
    app, api_client, clean_users, owner_connection
):
    """An empty string is not a question. Without `min_length=1` it creates a
    full billable turn and a persisted empty `messages` row -- on the one
    endpoint in this application that spends money."""
    token = await _register(api_client, "empty-message@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": ""}, headers=_auth(token)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    count = await owner_connection.execute(
        text("SELECT count(*) FROM conversations WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    assert count.scalar_one() == 0


async def test_an_oversized_message_is_rejected(app, api_client, clean_users):
    token = await _register(api_client, "huge-message@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "x" * (chat_api.MAX_MESSAGE_LENGTH + 1)},
        headers=_auth(token),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


async def test_a_message_at_the_size_limit_is_accepted(app, api_client, clean_users):
    """The counterpart to the test above: without it, `max_length` could be
    set to anything at all (1, say) and the suite would stay green."""
    token = await _register(api_client, "limit-message@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "x" * chat_api.MAX_MESSAGE_LENGTH},
        headers=_auth(token),
    )

    assert response.status_code == 200


async def test_chat_stream_is_rate_limited_per_user(app, api_client, clean_users, monkeypatch):
    """`register`/`login` are throttled; the only endpoint that costs money
    was not. Patched down to one request per window rather than actually
    driving 30 full turns -- the limit value is a constant precisely so this
    test does not have to spend a minute proving it."""
    monkeypatch.setattr(chat_api, "CHAT_RATE_LIMIT", 1)
    token = await _register(api_client, "chat-throttle@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    first = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )
    assert first.status_code == 200

    second = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello again"}, headers=_auth(token)
    )
    assert second.status_code == 429
    assert second.headers["content-type"].startswith("application/json")
    assert second.json()["error"]["code"] == "rate_limited"
    # Throttled before the stream is ever opened -- not a 200 carrying an
    # in-band error event.
    assert "text/event-stream" not in second.headers["content-type"]


async def test_a_second_user_is_not_throttled_by_the_firsts_traffic(
    app, api_client, clean_users, monkeypatch
):
    """Keyed per user, not globally or per IP: in this test every request
    comes from the same client address, so a key that was not user-scoped
    would throttle the second user out on their very first message."""
    monkeypatch.setattr(chat_api, "CHAT_RATE_LIMIT", 1)
    first_token = await _register(api_client, "throttle-user-one@example.com", "Ada Motors One")
    first_org = await _organization_id(api_client, first_token)
    first_agent = await _make_agent(first_org)
    second_token = await _register(api_client, "throttle-user-two@example.com", "Ada Motors Two")
    second_org = await _organization_id(api_client, second_token)
    second_agent = await _make_agent(second_org)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    for _ in range(2):
        await api_client.post(
            CHAT_URL,
            json={"agent_id": str(first_agent), "message": "hello"},
            headers=_auth(first_token),
        )

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(second_agent), "message": "hello"},
        headers=_auth(second_token),
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Extra: a non-`Exception` BaseException in the pump still terminates the stream
# ---------------------------------------------------------------------------


async def test_a_baseexception_in_the_pump_still_terminates_the_stream(monkeypatch):
    """`_pump` catches only `Exception`. Without a terminal sentinel on every
    exit path, a non-`Exception` `BaseException` from `events` leaves the
    consumer's `asyncio.wait_for(queue.get(), ...)` loop spinning forever --
    emitting `: ping` every heartbeat interval with no terminal event, so the
    client never learns the turn is over and the connection never closes.
    The session-close half of this was already fixed and tested; this is the
    half that hangs.
    """
    from app.api.chat import ChatMessageStart, _stream_body

    class _NoOpSessionCM:
        async def __aenter__(self) -> "_NoOpSessionCM":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    class _SimulatedBug(BaseException):
        """Not `SystemExit`/`KeyboardInterrupt`: `asyncio.Task` special-cases
        those two and re-raises them out of the event loop rather than
        storing them as the task's result."""

    async def _raises_unexpected_baseexception() -> AsyncIterator[object]:
        raise _SimulatedBug("simulated bug outside _pump's documented contract")
        yield  # pragma: no cover - unreachable; keeps this an async generator

    monkeypatch.setattr(chat_api, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    first_event = ChatMessageStart(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    body = _stream_body(
        _raises_unexpected_baseexception(), first_event, _NoOpSessionCM(), _NoNewConversation()
    )  # type: ignore[arg-type]

    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"data: ")
    # Let the pump task actually run and crash, so the queue's state is
    # settled before the consumer looks at it again -- otherwise a single
    # scheduling-order ping could appear even with the fix in place.
    await asyncio.sleep(0.05)

    chunks: list[bytes] = []

    async def _drain() -> None:
        async for chunk in body:
            chunks.append(chunk)

    # `_SimulatedBug` surfaces either way (it comes out of `await pump_task`
    # in `_stream_body`'s own cleanup, even when that cleanup is driven by
    # this `wait_for` cancelling the drain). The assertion that actually
    # discriminates is the one below: with a terminal sentinel the consumer
    # breaks out immediately and emits nothing more, while without one it
    # sits in `wait_for(queue.get(), ...)` emitting a heartbeat comment every
    # interval, forever.
    with pytest.raises(_SimulatedBug):
        await asyncio.wait_for(_drain(), timeout=2.0)

    assert chat_api._PING not in chunks  # noqa: SLF001


# ---------------------------------------------------------------------------
# 10. Per-request model / provider override (the playground's model picker)
# ---------------------------------------------------------------------------


async def test_model_override_is_what_reaches_the_provider(app, api_client, clean_users):
    token = await _register(api_client, "model-override@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="fake-1")
    provider = FakeProvider(script=["ok"])
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "model": "claude-sonnet-5"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert provider.last_request is not None
    assert provider.last_request.model == "claude-sonnet-5"


async def test_model_override_does_not_change_the_agent(app, api_client, clean_users):
    """The override is for one request. An implementation that wrote it back
    to the agent row would pass the test above and silently repoint every
    other channel this agent answers on."""
    token = await _register(api_client, "override-not-persisted@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="fake-1")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "model": "claude-sonnet-5"},
        headers=_auth(token),
    )

    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).get_agent(agent_id)
    assert agent.model == "fake-1"


async def test_message_end_reports_the_model_that_actually_answered(app, api_client, clean_users):
    """`message_end.model` is what the playground prints and what cost is
    estimated from, so it must follow the override rather than the agent."""
    token = await _register(api_client, "override-message-end@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="fake-1")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "model": "gpt-4o-mini"},
        headers=_auth(token),
    )

    end = next(e for e in _parse_events(response.text) if e["type"] == "message_end")
    assert end["model"] == "gpt-4o-mini"


async def test_the_overridden_provider_is_the_one_resolved(api_client, clean_users):
    """Deliberately no `get_chat_provider` override, so the real registry
    resolves the name. The agent is on `fake`, which always answers -- a 200
    here would mean the override never reached provider resolution. This
    environment has no `OPENAI_API_KEY`, so an override that *is* honoured
    fails the same way an agent configured for openai already does, before
    the stream is committed."""
    token = await _register(api_client, "provider-override@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, provider="fake", model="fake-1")

    response = await api_client.post(
        CHAT_URL,
        json={
            "agent_id": str(agent_id),
            "message": "hello",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        headers=_auth(token),
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "llm_misconfigured"
    assert "text/event-stream" not in response.headers["content-type"]


async def test_an_unknown_provider_override_is_rejected_as_json(app, api_client, clean_users):
    token = await _register(api_client, "unknown-provider-override@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "provider": "not-a-provider"},
        headers=_auth(token),
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert "text/event-stream" not in response.headers["content-type"]


async def test_an_empty_model_override_is_rejected(app, api_client, clean_users):
    """`""` is not "no override" -- without a length floor it reaches the
    provider as a blank model id and buys a 400 from the vendor."""
    token = await _register(api_client, "blank-model-override@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "model": ""},
        headers=_auth(token),
    )

    assert response.status_code == 422


async def test_without_an_override_the_agents_own_model_still_answers(app, api_client, clean_users):
    token = await _register(api_client, "no-override@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="fake-1")
    provider = FakeProvider(script=["ok"])
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 200, response.text
    assert provider.last_request is not None
    assert provider.last_request.model == "fake-1"


async def test_usage_event_records_the_model_that_answered(app, api_client, clean_users):
    """`usage_events` is the substrate billing reads. Recording the agent's
    configured model for a turn an overridden model was billed for would
    attribute real spend to the wrong model."""
    token = await _register(api_client, "override-usage@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id, model="fake-1")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "hello", "model": "claude-sonnet-5"},
        headers=_auth(token),
    )

    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        rows = (
            (
                await session.execute(
                    text("SELECT model FROM usage_events WHERE agent_id = :agent_id"),
                    {"agent_id": agent_id},
                )
            )
            .scalars()
            .all()
        )
    assert rows == ["claude-sonnet-5"]


# ---------------------------------------------------------------------------
# 11. Titling a new conversation
# ---------------------------------------------------------------------------


class _TitleRecorder:
    """Records each enqueue, and -- at the moment it happens -- whether the
    conversation is already visible to a *separate* transaction.

    That second fact is the point. The job runs in another process against
    its own session, so an enqueue that fires before this turn's transaction
    commits hands the worker a conversation it cannot see. Asserting commit
    ordering directly beats asserting on a sequence of recorded events,
    because this is the property the ordering exists to produce.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.visible: list[bool] = []

    async def __call__(self, conversation_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.calls.append((conversation_id, organization_id))
        tenant = TenantContext(
            organization_id=organization_id,
            user_id=None,
            role=MembershipRole.OWNER,
            request_id="title-visibility-probe",
        )
        async with tenant_session(tenant) as session:
            row = await session.execute(
                text("SELECT count(*) FROM messages WHERE conversation_id = :id"),
                {"id": conversation_id},
            )
            self.visible.append(row.scalar_one() > 0)


@pytest.fixture
def titles(monkeypatch) -> _TitleRecorder:
    recorder = _TitleRecorder()
    monkeypatch.setattr(chat_api, "enqueue_title", recorder)
    return recorder


async def test_a_first_playground_turn_queues_a_title(app, api_client, clean_users, titles):
    token = await _register(api_client, "title-first-turn@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    events = _parse_events(response.text)
    start = next(e for e in events if e["type"] == "message_start")
    conversation_id = uuid.UUID(str(start["conversation_id"]))
    assert titles.calls == [(conversation_id, org_id)]


async def test_the_title_is_queued_only_once_the_turn_has_committed(
    app, api_client, clean_users, titles
):
    """The job opens its own session in another process. Enqueued before this
    turn's transaction commits, it would find a conversation with no messages
    and have nothing to title."""
    token = await _register(api_client, "title-after-commit@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert titles.visible == [True]


async def test_a_second_turn_does_not_queue_another_title(app, api_client, clean_users, titles):
    """Only the turn that creates the conversation queues one. The job's own
    `title IS NULL` check is the backstop; not enqueuing is the first line."""
    token = await _register(api_client, "title-second-turn@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["ok"])

    first = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )
    conversation_id = next(e for e in _parse_events(first.text) if e["type"] == "message_start")[
        "conversation_id"
    ]

    await api_client.post(
        CHAT_URL,
        json={
            "agent_id": str(agent_id),
            "message": "and again",
            "conversation_id": str(conversation_id),
        },
        headers=_auth(token),
    )

    assert len(titles.calls) == 1


async def test_a_pre_stream_failure_queues_nothing(app, api_client, clean_users, titles):
    """No conversation was created, so there is nothing to title."""
    token = await _register(api_client, "title-pre-stream@example.com")
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(uuid.uuid4()), "message": "hello"},
        headers=_auth(token),
    )

    assert response.status_code == 404
    assert titles.calls == []


async def test_a_mid_stream_provider_error_still_queues_a_title(
    app, api_client, clean_users, titles
):
    """The partial reply IS persisted (see the mid-stream failure tests
    above), so the conversation is real, appears in the list, and needs a
    label like any other."""
    token = await _register(api_client, "title-mid-stream-error@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["partial ", "more"], fail_with=LLMUnavailableError("upstream down")
    )

    await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert len(titles.calls) == 1
    assert titles.visible == [True]


async def test_a_rolled_back_turn_queues_nothing(app, api_client, clean_users, titles, monkeypatch):
    """An unexpected exception rolls the whole turn back, taking the
    conversation row with it. Enqueuing would point the worker at a
    conversation that no longer exists."""
    token = await _register(api_client, "title-rolled-back@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)

    class _BrokenProvider:
        name = "fake"

        def capabilities(self, model: str) -> ModelCapabilities:
            return ModelCapabilities(
                supports_sampling=True,
                supports_thinking=False,
                thinking_style="none",
                supports_effort=False,
                max_output_tokens=4096,
            )

        async def generate(self, request: CompletionRequest) -> CompletionResponse:
            raise AssertionError("unused")

        async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            yield MessageStartEvent(model=request.model)
            yield TextDeltaEvent(text="hi")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(), model=request.model)
            raise RuntimeError("a bug after the turn finished")

        def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            return self._stream(request)

    app.dependency_overrides[chat_api.get_chat_provider] = lambda: _BrokenProvider()

    await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert titles.calls == []
