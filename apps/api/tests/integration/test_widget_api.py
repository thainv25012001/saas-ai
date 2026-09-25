"""The public widget API (spec §4, §4.1, §5): unauthenticated, internet
facing, anonymous visitors holding a 30-day token, spending real LLM money.

Review Focus 1 (another visitor's conversation), 2 (availability re-checked
on every bearer call) and 3 (nothing internal on the wire) are pinned here,
end to end through the real router."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from structlog.testing import capture_logs

from app.agents.schemas import UpdateAgentConfigInput, UpdateAgentInput
from app.agents.service import AgentService
from app.api import chat as chat_api
from app.api import widget as widget_api
from app.core.security import create_access_token
from app.core.tenancy import TenantContext, tenant_session
from app.db.models.widget import WidgetPosition
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.main import create_app
from app.widget import tokens as widget_tokens
from app.widget.schemas import UpdateWidgetSettingsInput
from app.widget.service import WidgetSettingsService
from app.widget.tokens import decode_visitor_token
from tests.conftest import _flush_rate_limits, enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

BASE = "/api/v1/widget"
CONVERSATION_URL = f"{BASE}/conversation"
STREAM_URL = f"{BASE}/chat/stream"
NOT_FOUND_BODY = {"error": {"code": "not_found", "message": "widget not found"}}


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def api_client(app) -> AsyncIterator[AsyncClient]:
    # Every test here shares one client IP (ASGITransport's), so the per-IP
    # widget counters must start from zero or earlier tests would trip them.
    await _flush_rate_limits()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await _flush_rate_limits()


@pytest.fixture
def fake_provider(app):
    """A fresh `FakeProvider` per request (the dependency is re-evaluated each
    call), so a test can send several turns with one script."""

    def _use(factory):  # type: ignore[no-untyped-def]
        app.dependency_overrides[chat_api.get_chat_provider] = factory

    _use(lambda: FakeProvider(script=["Hel", "lo!"]))
    return _use


def _parse_events(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_widget(
    tenant: TenantContext,
    *,
    status: str = "active",
    enabled: bool = True,
    cap: int = 500,
    name: str = "Sales Bot",
) -> tuple[uuid.UUID, str]:
    async with tenant_session(tenant) as session:
        agents = AgentService(session, tenant)
        agent = await agents.create_agent(agent_input(name))
        if status != "draft":
            await agents.update_agent(agent.id, UpdateAgentInput(status=status))  # type: ignore[arg-type]
        await agents.update_config(
            agent.id,
            UpdateAgentConfigInput(greeting="Hi! Ask me anything.", fallback_message="Sorry!"),
        )
        await WidgetSettingsService(session, tenant).update(
            agent.id,
            UpdateWidgetSettingsInput(
                enabled=enabled,
                allowed_origins=["https://shop.example.com"],
                brand_color="#123ABC",
                position=WidgetPosition.LEFT,
                title="Chat with us",
                daily_message_cap=cap,
            ),
        )
        return agent.id, agent.public_key


async def _set_enabled(tenant: TenantContext, agent_id: uuid.UUID, enabled: bool) -> None:
    async with tenant_session(tenant) as session:
        await WidgetSettingsService(session, tenant).update(
            agent_id,
            UpdateWidgetSettingsInput(
                enabled=enabled,
                allowed_origins=["https://shop.example.com"],
                brand_color="#123abc",
                position=WidgetPosition.LEFT,
                title="Chat with us",
                daily_message_cap=500,
            ),
        )


async def _set_status(tenant: TenantContext, agent_id: uuid.UUID, status: str) -> None:
    async with tenant_session(tenant) as session:
        await AgentService(session, tenant).update_agent(
            agent_id,
            UpdateAgentInput(status=status),  # type: ignore[arg-type]
        )


async def _session(
    client: AsyncClient, public_key: str, token: str | None = None
) -> dict[str, object]:
    response = await client.post(
        f"{BASE}/{public_key}/session", headers=_bearer(token) if token else {}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def _token(client: AsyncClient, public_key: str) -> str:
    return str((await _session(client, public_key))["token"])


async def _send(
    client: AsyncClient, token: str, message: str, conversation_id: object = None
) -> tuple[int, list[dict[str, object]], str]:
    payload: dict[str, object] = {"message": message}
    if conversation_id is not None:
        payload["conversation_id"] = str(conversation_id)
    response = await client.post(STREAM_URL, json=payload, headers=_bearer(token))
    return response.status_code, _parse_events(response.text), response.text


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


async def test_session_mints_a_token_and_returns_the_public_config(api_client, tenant_a):
    agent_id, public_key = await _seed_widget(tenant_a)

    body = await _session(api_client, public_key)

    assert set(body) == {"token", "expires_at", "config"}
    assert body["config"] == {
        "agent_name": "Sales Bot",
        "title": "Chat with us",
        "greeting": "Hi! Ask me anything.",
        "fallback_message": "Sorry!",
        "brand_color": "#123abc",
        "position": "left",
    }
    claims = decode_visitor_token(str(body["token"]))
    assert claims.organization_id == tenant_a.organization_id
    assert claims.agent_id == agent_id
    uuid.UUID(claims.visitor_id)  # a random UUID4 string


async def test_replaying_the_session_with_its_token_keeps_the_visitor_id(api_client, tenant_a):
    _agent_id, public_key = await _seed_widget(tenant_a)

    first = await _token(api_client, public_key)
    second = str((await _session(api_client, public_key, first))["token"])

    assert decode_visitor_token(first).visitor_id == decode_visitor_token(second).visitor_id


async def test_a_token_for_another_agent_mints_a_new_visitor_id(api_client, tenant_a):
    _a, key_a = await _seed_widget(tenant_a, name="Bot A")
    _b, key_b = await _seed_widget(tenant_a, name="Bot B")

    token_a = await _token(api_client, key_a)
    token_b = str((await _session(api_client, key_b, token_a))["token"])

    assert decode_visitor_token(token_a).visitor_id != decode_visitor_token(token_b).visitor_id


async def test_a_garbage_bearer_on_session_just_mints_a_new_token(api_client, tenant_a):
    _agent_id, public_key = await _seed_widget(tenant_a)

    body = await _session(api_client, public_key, "garbage")

    decode_visitor_token(str(body["token"]))


async def test_every_session_refusal_is_the_identical_404(api_client, tenant_a):
    _d, draft_key = await _seed_widget(tenant_a, status="draft", name="Draft")
    _x, disabled_key = await _seed_widget(tenant_a, status="disabled", name="Disabled")
    _o, off_key = await _seed_widget(tenant_a, enabled=False, name="Off")

    for key in (
        "pk_unknownunknownunknown00",
        "not-a-key",
        draft_key,
        disabled_key,
        off_key,
    ):
        response = await api_client.post(f"{BASE}/{key}/session")
        assert response.status_code == 404, key
        assert response.json() == NOT_FOUND_BODY


# ---------------------------------------------------------------------------
# Review Focus 2: availability is re-checked on every bearer call
# ---------------------------------------------------------------------------


async def test_turning_the_widget_off_or_the_agent_to_draft_404s_a_live_token(
    api_client, tenant_a, fake_provider
):
    agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)
    status, _events, _body = await _send(api_client, token, "hello")
    assert status == 200

    await _set_enabled(tenant_a, agent_id, False)

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    response = await api_client.post(STREAM_URL, json={"message": "hi"}, headers=_bearer(token))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    response = await api_client.post(f"{BASE}/{public_key}/session", headers=_bearer(token))
    assert response.status_code == 404

    await _set_enabled(tenant_a, agent_id, True)
    assert (await api_client.get(CONVERSATION_URL, headers=_bearer(token))).status_code == 200

    await _set_status(tenant_a, agent_id, "draft")

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    response = await api_client.post(STREAM_URL, json={"message": "hi"}, headers=_bearer(token))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


# ---------------------------------------------------------------------------
# tokens: never interchangeable with dashboard tokens
# ---------------------------------------------------------------------------


async def test_a_dashboard_access_token_is_refused_by_the_widget(api_client, tenant_a):
    await _seed_widget(tenant_a)
    access = create_access_token(
        user_id=tenant_a.user_id,  # type: ignore[arg-type]
        organization_id=tenant_a.organization_id,
        role="owner",
    )

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(access))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    response = await api_client.post(STREAM_URL, json={"message": "hi"}, headers=_bearer(access))
    assert response.status_code == 401


async def test_a_widget_token_is_refused_by_the_dashboard_chat_and_graphql(
    api_client, tenant_a, fake_provider
):
    agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)

    response = await api_client.post(
        "/api/v1/chat/stream",
        json={"agent_id": str(agent_id), "message": "hi"},
        headers=_bearer(token),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"

    response = await api_client.post(
        "/graphql", json={"query": "{ me { email } }"}, headers=_bearer(token)
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"
    assert response.json().get("data") is None


async def test_an_expired_widget_token_is_401(api_client, tenant_a, monkeypatch):
    agent_id, _public_key = await _seed_widget(tenant_a)
    monkeypatch.setattr(widget_tokens, "_token_lifetime", lambda: timedelta(seconds=-60))
    expired, _ = widget_tokens.create_visitor_token(
        organization_id=tenant_a.organization_id, agent_id=agent_id, visitor_id="v-1"
    )

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(expired))
    assert response.status_code == 401
    response = await api_client.post(STREAM_URL, json={"message": "hi"}, headers=_bearer(expired))
    assert response.status_code == 401


@pytest.mark.parametrize("header", [None, "Bearer garbage", "Basic abc", "Bearer "])
async def test_a_missing_or_garbage_bearer_is_401(api_client, header):
    headers = {"Authorization": header} if header is not None else {}
    response = await api_client.get(CONVERSATION_URL, headers=headers)
    assert response.status_code == 401
    response = await api_client.post(STREAM_URL, json={"message": "hi"}, headers=headers)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Review Focus 1: another visitor's conversation
# ---------------------------------------------------------------------------


async def test_a_visitor_cannot_stream_into_or_resume_another_visitors_conversation(
    api_client, tenant_a, fake_provider, owner_connection
):
    agent_id, public_key = await _seed_widget(tenant_a)
    token_a = await _token(api_client, public_key)
    token_b = await _token(api_client, public_key)
    assert decode_visitor_token(token_a).visitor_id != decode_visitor_token(token_b).visitor_id

    status, events, _ = await _send(api_client, token_a, "A's question")
    assert status == 200
    conversation_id = events[0]["conversation_id"]

    response = await api_client.post(
        STREAM_URL,
        json={"message": "B sneaks in", "conversation_id": conversation_id},
        headers=_bearer(token_b),
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")

    rows = (
        await owner_connection.execute(
            text("SELECT role, content FROM messages WHERE conversation_id = :id ORDER BY seq"),
            {"id": uuid.UUID(str(conversation_id))},
        )
    ).all()
    assert [(r.role, r.content) for r in rows] == [
        ("user", "A's question"),
        ("assistant", "Hello!"),
    ]

    b_view = await api_client.get(CONVERSATION_URL, headers=_bearer(token_b))
    assert b_view.status_code == 200
    assert b_view.json() is None

    a_view = await api_client.get(CONVERSATION_URL, headers=_bearer(token_a))
    assert a_view.json()["conversation_id"] == conversation_id


async def test_the_same_visitors_conversation_on_another_agent_is_404(
    api_client, tenant_a, fake_provider
):
    _a, key_a = await _seed_widget(tenant_a, name="Bot A")
    agent_b, key_b = await _seed_widget(tenant_a, name="Bot B")
    token_a = await _token(api_client, key_a)
    status, events, _ = await _send(api_client, token_a, "hello")
    assert status == 200
    conversation_id = events[0]["conversation_id"]

    # The same visitor id holding a token for agent B too (minted directly:
    # the session route would give B a fresh vid).
    claims_a = decode_visitor_token(token_a)
    token_b, _ = widget_tokens.create_visitor_token(
        organization_id=tenant_a.organization_id,
        agent_id=agent_b,
        visitor_id=claims_a.visitor_id,
    )
    await _session(api_client, key_b)  # B is available

    response = await api_client.post(
        STREAM_URL,
        json={"message": "cross agent", "conversation_id": conversation_id},
        headers=_bearer(token_b),
    )
    assert response.status_code == 404
    b_view = await api_client.get(CONVERSATION_URL, headers=_bearer(token_b))
    assert b_view.json() is None


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


async def test_conversation_returns_both_turns_in_order(api_client, tenant_a, fake_provider):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)

    empty = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    assert empty.status_code == 200
    assert empty.json() is None

    _s, events, _ = await _send(api_client, token, "first")
    conversation_id = events[0]["conversation_id"]
    fake_provider(lambda: FakeProvider(script=["second ", "answer"]))
    _s, events2, _ = await _send(api_client, token, "second", conversation_id)
    assert events2[0]["conversation_id"] == conversation_id

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": conversation_id,
        "messages": [
            {"role": "user", "text": "first"},
            {"role": "assistant", "text": "Hello!"},
            {"role": "user", "text": "second"},
            {"role": "assistant", "text": "second answer"},
        ],
    }


async def test_a_closed_conversation_is_not_resumed(
    api_client, tenant_a, fake_provider, owner_connection
):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)
    _s, events, _ = await _send(api_client, token, "first")
    conversation_id = uuid.UUID(str(events[0]["conversation_id"]))

    await owner_connection.execute(
        text("UPDATE conversations SET status = 'closed', closed_at = now() WHERE id = :id"),
        {"id": conversation_id},
    )
    await owner_connection.commit()

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    assert response.status_code == 200
    assert response.json() is None


async def test_conversation_skips_tool_rows_and_empty_assistant_text(
    api_client, tenant_a, fake_provider
):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)
    fake_provider(
        lambda: FakeProvider(
            turns=[
                [FakeToolCall(name="search_products", input={"query": "shoes"})],
                "We have shoes.",
            ]
        )
    )
    status, _events, _ = await _send(api_client, token, "shoes?")
    assert status == 200

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(token))
    messages = response.json()["messages"]
    assert messages[0] == {"role": "user", "text": "shoes?"}
    assert messages[-1] == {"role": "assistant", "text": "We have shoes."}
    assert all(m["role"] in ("user", "assistant") and m["text"] for m in messages)


# ---------------------------------------------------------------------------
# Review Focus 3: nothing internal on the wire
# ---------------------------------------------------------------------------


async def test_a_tool_using_turn_leaks_no_arguments_results_or_costs(
    api_client, tenant_a, fake_provider
):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)
    fake_provider(
        lambda: FakeProvider(
            turns=[
                [FakeToolCall(name="search_products", input={"query": "secret-query"})],
                "Here is what I found.",
            ]
        )
    )

    status, events, body = await _send(api_client, token, "find shoes")

    assert status == 200
    for forbidden in ('"arguments"', '"result"', '"excerpt"', '"cost_usd"', '"model"'):
        assert forbidden not in body, forbidden
    assert "secret-query" not in body
    starts = [e for e in events if e["type"] == "tool_call_start"]
    assert starts and {"name": "search_products"} in starts[0]["calls"]  # type: ignore[operator]
    assert "tool_call_end" not in [e["type"] for e in events]
    assert events[-1] == {"type": "message_end"}


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


async def test_the_per_visitor_message_limit_answers_429(
    api_client, tenant_a, fake_provider, monkeypatch
):
    monkeypatch.setattr(widget_api, "VISITOR_MESSAGE_LIMIT", 2)
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)

    assert (await _send(api_client, token, "one"))[0] == 200
    assert (await _send(api_client, token, "two"))[0] == 200
    response = await api_client.post(STREAM_URL, json={"message": "three"}, headers=_bearer(token))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"

    # Another visitor on the same agent is not throttled by the first.
    other = await _token(api_client, public_key)
    assert (await _send(api_client, other, "hi"))[0] == 200


async def test_the_per_ip_message_limit_answers_429(
    api_client, tenant_a, fake_provider, monkeypatch
):
    monkeypatch.setattr(widget_api, "IP_MESSAGE_LIMIT", 1)
    _agent_id, public_key = await _seed_widget(tenant_a)
    first = await _token(api_client, public_key)
    second = await _token(api_client, public_key)

    assert (await _send(api_client, first, "one"))[0] == 200
    response = await api_client.post(STREAM_URL, json={"message": "two"}, headers=_bearer(second))
    assert response.status_code == 429


async def test_the_daily_cap_answers_429_widget_daily_cap(api_client, tenant_a, fake_provider):
    _agent_id, public_key = await _seed_widget(tenant_a, cap=1)
    token = await _token(api_client, public_key)

    assert (await _send(api_client, token, "one"))[0] == 200
    other = await _token(api_client, public_key)
    response = await api_client.post(STREAM_URL, json={"message": "two"}, headers=_bearer(other))

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "widget_daily_cap"


async def test_the_new_session_limit_counts_only_minted_tokens(api_client, tenant_a, monkeypatch):
    monkeypatch.setattr(widget_api, "SESSION_MINT_LIMIT", 2)
    _agent_id, public_key = await _seed_widget(tenant_a)

    token = await _token(api_client, public_key)  # mint 1
    for _ in range(5):  # refreshes are not counted
        token = str((await _session(api_client, public_key, token))["token"])
    await _token(api_client, public_key)  # mint 2

    response = await api_client.post(f"{BASE}/{public_key}/session")  # mint 3
    assert response.status_code == 429
    # ... while a visitor who already has a token can still refresh it.
    await _session(api_client, public_key, token)


async def test_frame_policy_is_rate_limited_per_key(api_client, tenant_a, monkeypatch):
    monkeypatch.setattr(widget_api, "FRAME_POLICY_LIMIT", 1)
    _agent_id, public_key = await _seed_widget(tenant_a)

    assert (await api_client.get(f"{BASE}/{public_key}/frame-policy")).status_code == 200
    assert (await api_client.get(f"{BASE}/{public_key}/frame-policy")).status_code == 429


async def test_a_limited_frame_policy_key_leaves_other_keys_alone(
    api_client, tenant_a, monkeypatch
):
    # Every request comes from the one web server, so a per-IP limit would
    # let one key (or a flood of made-up ones) unframe every other widget.
    monkeypatch.setattr(widget_api, "FRAME_POLICY_LIMIT", 1)
    _a, flooded_key = await _seed_widget(tenant_a, name="Flooded")
    _b, other_key = await _seed_widget(tenant_a, name="Other")

    await api_client.get(f"{BASE}/{flooded_key}/frame-policy")
    assert (await api_client.get(f"{BASE}/{flooded_key}/frame-policy")).status_code == 429

    other = await api_client.get(f"{BASE}/{other_key}/frame-policy")
    assert other.status_code == 200
    assert other.json() == {"allowed_origins": ["https://shop.example.com"]}


@pytest.fixture
def frame_secret(monkeypatch):
    """Configure `WIDGET_FRAME_POLICY_SECRET` for one test."""
    from app.core.config import get_settings

    def _set(value: str | None) -> None:
        settings = get_settings().model_copy(update={"widget_frame_policy_secret": value})
        monkeypatch.setattr(widget_api, "get_settings", lambda: settings)

    return _set


async def test_frame_policy_with_the_configured_secret_is_never_limited(
    api_client, tenant_a, monkeypatch, frame_secret
):
    # The key is public: without this, ~2 requests/s from anyone would keep
    # its budget spent and cold middleware instances would unframe it.
    monkeypatch.setattr(widget_api, "FRAME_POLICY_LIMIT", 1)
    frame_secret("s3cret-value")
    _agent_id, public_key = await _seed_widget(tenant_a)
    headers = {"X-Widget-Frame-Secret": "s3cret-value"}

    for _ in range(4):
        response = await api_client.get(f"{BASE}/{public_key}/frame-policy", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"allowed_origins": ["https://shop.example.com"]}


@pytest.mark.parametrize("header", [None, "wrong-value", ""])
async def test_frame_policy_with_a_wrong_or_missing_secret_is_limited(
    api_client, tenant_a, monkeypatch, frame_secret, header
):
    monkeypatch.setattr(widget_api, "FRAME_POLICY_LIMIT", 1)
    frame_secret("s3cret-value")
    _agent_id, public_key = await _seed_widget(tenant_a)
    headers = {} if header is None else {"X-Widget-Frame-Secret": header}

    url = f"{BASE}/{public_key}/frame-policy"
    assert (await api_client.get(url, headers=headers)).status_code == 200
    assert (await api_client.get(url, headers=headers)).status_code == 429


async def test_frame_policy_ignores_the_secret_header_when_no_secret_is_configured(
    api_client, tenant_a, monkeypatch, frame_secret
):
    monkeypatch.setattr(widget_api, "FRAME_POLICY_LIMIT", 1)
    frame_secret(None)
    _agent_id, public_key = await _seed_widget(tenant_a)
    headers = {"X-Widget-Frame-Secret": "anything"}

    url = f"{BASE}/{public_key}/frame-policy"
    assert (await api_client.get(url, headers=headers)).status_code == 200
    assert (await api_client.get(url, headers=headers)).status_code == 429


async def test_frame_policy_answers_a_malformed_key_before_the_limiter(api_client, monkeypatch):
    """A garbage key never touches Redis or the database."""

    async def _boom(*_args, **_kwargs):
        raise AssertionError("the limiter must not be reached")

    monkeypatch.setattr(widget_api, "enforce_rate_limit", _boom)
    monkeypatch.setattr(widget_api, "resolve_public_key", _boom)

    response = await api_client.get(f"{BASE}/not-a-key/frame-policy")
    assert response.status_code == 200
    assert response.json() == {"allowed_origins": []}


# ---------------------------------------------------------------------------
# launcher config
# ---------------------------------------------------------------------------

UNAVAILABLE_CONFIG = {"available": False, "brand_color": None, "position": None, "title": None}


async def test_config_describes_an_available_widget(api_client, tenant_a):
    _agent_id, public_key = await _seed_widget(tenant_a)

    response = await api_client.get(f"{BASE}/{public_key}/config")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "brand_color": "#123abc",
        "position": "left",
        "title": "Chat with us",
    }
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["access-control-allow-origin"] == "*"


async def test_config_is_all_nulls_for_anything_unavailable(api_client, tenant_a):
    _d, draft_key = await _seed_widget(tenant_a, status="draft", name="Draft")
    _o, off_key = await _seed_widget(tenant_a, enabled=False, name="Off")

    for key in (draft_key, off_key, "pk_unknownunknownunknown00"):
        response = await api_client.get(f"{BASE}/{key}/config")
        assert response.status_code == 200, key
        assert response.json() == UNAVAILABLE_CONFIG
        assert response.headers["access-control-allow-origin"] == "*"


async def test_config_answers_a_malformed_key_before_the_limiter(api_client, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise AssertionError("the limiter must not be reached")

    monkeypatch.setattr(widget_api, "enforce_rate_limit", _boom)
    monkeypatch.setattr(widget_api, "resolve_public_key", _boom)

    response = await api_client.get(f"{BASE}/not-a-key/config")
    assert response.status_code == 200
    assert response.json() == UNAVAILABLE_CONFIG


async def test_config_is_rate_limited_per_key(api_client, tenant_a, monkeypatch):
    monkeypatch.setattr(widget_api, "CONFIG_LIMIT", 1)
    _a, public_key = await _seed_widget(tenant_a, name="A")
    _b, other_key = await _seed_widget(tenant_a, name="B")

    assert (await api_client.get(f"{BASE}/{public_key}/config")).status_code == 200
    assert (await api_client.get(f"{BASE}/{public_key}/config")).status_code == 429
    assert (await api_client.get(f"{BASE}/{other_key}/config")).status_code == 200


async def test_config_cors_header_survives_the_global_cors_middleware(api_client, tenant_a):
    """The global CORSMiddleware (credentials on, an allow-list) must neither
    strip nor rewrite the wildcard for a page on any site."""
    _agent_id, public_key = await _seed_widget(tenant_a)

    response = await api_client.get(
        f"{BASE}/{public_key}/config", headers={"Origin": "https://random.example"}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.json()["available"] is True


# ---------------------------------------------------------------------------
# message validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["", "x" * 2001])
async def test_message_length_is_bounded(api_client, tenant_a, fake_provider, message):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)

    response = await api_client.post(STREAM_URL, json={"message": message}, headers=_bearer(token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


async def test_a_2000_character_message_is_accepted(api_client, tenant_a, fake_provider):
    _agent_id, public_key = await _seed_widget(tenant_a)
    token = await _token(api_client, public_key)

    assert (await _send(api_client, token, "x" * 2000))[0] == 200


# ---------------------------------------------------------------------------
# frame-policy
# ---------------------------------------------------------------------------


async def test_frame_policy_returns_origins_only_when_available(api_client, tenant_a):
    _a, live_key = await _seed_widget(tenant_a, name="Live")
    _d, draft_key = await _seed_widget(tenant_a, status="draft", name="Draft")
    _o, off_key = await _seed_widget(tenant_a, enabled=False, name="Off")

    live = await api_client.get(f"{BASE}/{live_key}/frame-policy")
    assert live.status_code == 200
    assert live.json() == {"allowed_origins": ["https://shop.example.com"]}

    for key in (draft_key, off_key, "pk_unknownunknownunknown00", "nope"):
        response = await api_client.get(f"{BASE}/{key}/frame-policy")
        assert response.status_code == 200, key
        assert response.json() == {"allowed_origins": []}


# ---------------------------------------------------------------------------
# leads
# ---------------------------------------------------------------------------


async def test_a_lead_created_through_the_widget_is_sourced_widget(
    api_client, tenant_a, fake_provider, owner_connection
):
    agent_id, public_key = await _seed_widget(tenant_a)
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, "create_lead")
    token = await _token(api_client, public_key)
    fake_provider(
        lambda: FakeProvider(
            turns=[
                [
                    FakeToolCall(
                        name="create_lead",
                        input={
                            "name": "Visitor",
                            "email": "visitor@example.com",
                            "interest": "pricing",
                        },
                    )
                ],
                "Thanks, we'll be in touch.",
            ]
        )
    )

    status, events, _ = await _send(api_client, token, "call me")

    assert status == 200
    conversation_id = uuid.UUID(str(events[0]["conversation_id"]))
    row = (
        await owner_connection.execute(
            text(
                "SELECT l.source, c.channel, c.visitor_id FROM leads l "
                "JOIN conversations c ON c.id = l.conversation_id "
                "WHERE l.conversation_id = :id"
            ),
            {"id": conversation_id},
        )
    ).one()
    assert row.source == "widget"
    assert row.channel == "widget"
    assert row.visitor_id == decode_visitor_token(token).visitor_id


# ---------------------------------------------------------------------------
# privacy: logs
# ---------------------------------------------------------------------------


async def test_logs_carry_neither_the_visitor_id_nor_the_full_public_key(
    api_client, tenant_a, fake_provider
):
    _agent_id, public_key = await _seed_widget(tenant_a)
    _d, draft_key = await _seed_widget(tenant_a, status="draft", name="Draft")

    with capture_logs() as entries:
        token = await _token(api_client, public_key)
        vid = decode_visitor_token(token).visitor_id
        _s, events, _ = await _send(api_client, token, "a private question")
        await api_client.get(CONVERSATION_URL, headers=_bearer(token))
        await api_client.get(f"{BASE}/{public_key}/frame-policy")
        await api_client.post(f"{BASE}/{draft_key}/session")
        await api_client.post(
            STREAM_URL,
            json={"message": "x", "conversation_id": str(uuid.uuid4())},
            headers=_bearer(token),
        )

    assert entries, "expected at least the access-log lines"
    dumped = repr(entries)
    assert vid not in dumped
    assert public_key not in dumped
    assert draft_key not in dumped
    assert "a private question" not in dumped
    assert "127.0.0.1" not in dumped
    # The rejection is still logged -- by its 8-character prefix.
    assert any(e.get("public_key_prefix") == draft_key[:8] for e in entries)


async def test_the_other_tenants_agent_is_never_reachable_through_a_token(
    api_client, tenant_a, tenant_b, fake_provider
):
    """A token names its org and agent; both are re-resolved under that org's
    RLS every call. A token claiming tenant B's org for tenant A's agent (only
    possible with the signing secret, but the server must not rely on the
    pairing being honest) finds nothing."""
    agent_id, _key = await _seed_widget(tenant_a)
    forged, _ = widget_tokens.create_visitor_token(
        organization_id=tenant_b.organization_id, agent_id=agent_id, visitor_id="v-x"
    )

    response = await api_client.get(CONVERSATION_URL, headers=_bearer(forged))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
