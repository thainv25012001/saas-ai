import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agents.schemas import CreateAgentInput
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
        agent = await AgentService(session, tenant).create_agent(
            CreateAgentInput(name=name, **overrides)  # type: ignore[arg-type]
        )
    return agent.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
    """
    token = await _register(api_client, "unexpected-failure@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    provider = FakeProvider(
        script=["partial ", "output that never completes"],
        fail_with=RuntimeError("boom"),
    )
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    assert response.status_code == 200
    events = _parse_events(response.text)
    assert events[0]["type"] == "message_start"
    delta_index = next(i for i, e in enumerate(events) if e["type"] == "text_delta")
    error_index = next(i for i, e in enumerate(events) if e["type"] == "error")
    assert error_index > delta_index
    error_event = events[error_index]
    assert error_event["code"] == "internal_error"
    assert "boom" in str(error_event["message"])
    assert error_index == len(events) - 1


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
    body = _stream_body(_hangs_forever(), first_event, session_cm)  # type: ignore[arg-type]

    first_chunk = await body.__anext__()
    assert first_chunk.startswith(b"data: ")

    await body.aclose()

    assert session_cm.exit_args is not None
    exc_type, exc, _tb = session_cm.exit_args
    assert exc_type is GeneratorExit
