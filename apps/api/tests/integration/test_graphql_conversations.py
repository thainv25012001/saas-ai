"""`conversations` and `conversation` -- the GraphQL read side of playground
chat history.

Rows are seeded directly through `ConversationService` rather than by driving
`/api/v1/chat/stream`: these tests are about what the read surface returns,
and going through the chat endpoint would drag a provider, a rate limiter and
an SSE parse into every one of them.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.conversations.schemas import AppendMessageInput, CreateConversationInput
from app.conversations.service import ConversationService
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel, MembershipRole, MessageRole
from app.main import create_app
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean(clean_users) -> None:
    """Every test here registers an account; `clean_users` also flushes the
    register endpoint's rate limiter, which this module would otherwise
    exhaust partway through."""
    return None


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def _register(api_client: AsyncClient, email: str, org_name: str = "Ada Motors") -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "History Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


async def _agent(org_id: uuid.UUID) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input("Sales Bot"))
    return agent.id


async def _conversation(
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    channel: ConversationChannel = ConversationChannel.PLAYGROUND,
    title: str | None = None,
) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        conversation = await ConversationService(session, tenant).create(
            agent_id, CreateConversationInput(channel=channel)
        )
        if title is not None:
            conversation.title = title
    return conversation.id


CONVERSATIONS_QUERY = """
query Conversations($agentId: UUID!, $channel: ConversationChannel) {
  conversations(agentId: $agentId, channel: $channel) {
    id title channel status lastMessageAt createdAt
  }
}
"""


async def test_conversations_lists_an_agents_threads(api_client):
    token = await _register(api_client, "list-conversations@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id, title="Pricing questions")

    response = await graphql(
        api_client, CONVERSATIONS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )

    body = response.json()
    assert "errors" not in body, body
    rows = body["data"]["conversations"]
    assert [row["id"] for row in rows] == [str(conversation_id)]
    assert rows[0]["title"] == "Pricing questions"
    assert rows[0]["channel"] == "PLAYGROUND"
    assert rows[0]["status"] == "OPEN"


async def test_conversations_filters_by_channel(api_client):
    token = await _register(api_client, "filter-conversations@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    playground_id = await _conversation(org_id, agent_id, channel=ConversationChannel.PLAYGROUND)
    await _conversation(org_id, agent_id, channel=ConversationChannel.API)

    response = await graphql(
        api_client,
        CONVERSATIONS_QUERY,
        {"agentId": str(agent_id), "channel": "PLAYGROUND"},
        _auth(token),
    )

    rows = response.json()["data"]["conversations"]
    assert [row["id"] for row in rows] == [str(playground_id)]


async def test_conversations_for_another_organizations_agent_is_empty(api_client):
    """Not an error: an agent id that is not yours is indistinguishable from
    one that does not exist, and saying which would confirm it exists."""
    owner_token = await _register(api_client, "owner-org-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)
    await _conversation(owner_org_id, agent_id)

    other_token = await _register(api_client, "owner-org-b@example.com", "Org B")

    response = await graphql(
        api_client, CONVERSATIONS_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["conversations"] == []


async def test_conversations_requires_authentication(api_client):
    response = await graphql(api_client, CONVERSATIONS_QUERY, {"agentId": str(uuid.uuid4())})

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


CONVERSATION_QUERY = """
query Conversation($id: UUID!) {
  conversation(id: $id) {
    id
    title
    messages { id seq role content model provider costUsd latencyMs error }
  }
}
"""


async def test_conversation_returns_its_transcript_oldest_first(api_client):
    token = await _register(api_client, "transcript@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = ConversationService(session, tenant)
        await service.append_message(
            conversation_id, AppendMessageInput(role=MessageRole.USER, content="how much?")
        )
        await service.append_message(
            conversation_id,
            AppendMessageInput(
                role=MessageRole.ASSISTANT,
                content="Forty dollars.",
                model="gpt-4o-mini",
                provider="openai",
                latency_ms=1840,
            ),
        )

    response = await graphql(
        api_client, CONVERSATION_QUERY, {"id": str(conversation_id)}, _auth(token)
    )

    body = response.json()
    assert "errors" not in body, body
    messages = body["data"]["conversation"]["messages"]
    assert [m["role"] for m in messages] == ["USER", "ASSISTANT"]
    assert [m["seq"] for m in messages] == [1, 2]
    assert messages[1]["content"] == "Forty dollars."
    assert messages[1]["model"] == "gpt-4o-mini"
    assert messages[1]["latencyMs"] == 1840


async def test_conversation_returns_a_failed_turn_rather_than_skipping_it(api_client):
    """A turn that died before producing text has `content IS NULL` and
    `error` set. Dropping it would make the reopened transcript disagree with
    what the user watched happen."""
    token = await _register(api_client, "failed-turn@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        await ConversationService(session, tenant).append_message(
            conversation_id,
            AppendMessageInput(role=MessageRole.ASSISTANT, content=None, error="llm_unavailable"),
        )

    response = await graphql(
        api_client, CONVERSATION_QUERY, {"id": str(conversation_id)}, _auth(token)
    )

    messages = response.json()["data"]["conversation"]["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] is None
    assert messages[0]["error"] == "llm_unavailable"


async def test_conversation_from_another_organization_is_null(api_client):
    """Null, like `document(id)` -- for the dashboard "not yours" and "does
    not exist" are both nothing to show, and an error that distinguished them
    would confirm the row exists."""
    owner_token = await _register(api_client, "convo-org-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)
    conversation_id = await _conversation(owner_org_id, agent_id)

    other_token = await _register(api_client, "convo-org-b@example.com", "Org B")

    response = await graphql(
        api_client, CONVERSATION_QUERY, {"id": str(conversation_id)}, _auth(other_token)
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["conversation"] is None


async def test_citations_for_a_whole_transcript_are_fetched_in_one_query(api_client):
    """The N+1 this loader exists to prevent.

    Asserted by counting the statements Postgres is actually asked to run,
    not by reading the resolver: a version that resolves `citations`
    per-message returns byte-identical data and passes every other test in
    this file while issuing one query per message.
    """
    from sqlalchemy import event

    from app.db.models import MessageCitation
    from app.db.session import engine

    token = await _register(api_client, "citation-batching@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)

    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = ConversationService(session, tenant)
        for index in range(4):
            message = await service.append_message(
                conversation_id,
                AppendMessageInput(role=MessageRole.ASSISTANT, content=f"answer {index}"),
            )
            session.add(
                MessageCitation(
                    organization_id=org_id,
                    message_id=message.id,
                    chunk_id=None,
                    document_id=None,
                    document_title=f"Doc {index}",
                    excerpt="...",
                    rank=1,
                    score=0.9,
                )
            )

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        if "message_citations" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await graphql(
            api_client,
            """
            query Citations($id: UUID!) {
              conversation(id: $id) {
                messages { id citations { documentTitle rank } }
              }
            }
            """,
            {"id": str(conversation_id)},
            _auth(token),
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    body = response.json()
    assert "errors" not in body, body
    messages = body["data"]["conversation"]["messages"]
    assert len(messages) == 4
    assert [m["citations"][0]["documentTitle"] for m in messages] == [
        "Doc 0",
        "Doc 1",
        "Doc 2",
        "Doc 3",
    ]
    assert len(statements) == 1, f"expected one batched query, got {len(statements)}"


async def test_a_message_without_citations_gets_an_empty_list(api_client):
    """DataLoader matches results to keys positionally, so a message with no
    citations must still contribute a slot. Dropping it shifts every later
    message's citations onto the wrong message."""
    token = await _register(api_client, "citation-gaps@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)

    from app.db.models import MessageCitation

    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = ConversationService(session, tenant)
        await service.append_message(
            conversation_id, AppendMessageInput(role=MessageRole.USER, content="ungrounded")
        )
        grounded = await service.append_message(
            conversation_id, AppendMessageInput(role=MessageRole.ASSISTANT, content="grounded")
        )
        session.add(
            MessageCitation(
                organization_id=org_id,
                message_id=grounded.id,
                chunk_id=None,
                document_id=None,
                document_title="Pricing",
                excerpt="...",
                rank=1,
                score=0.9,
            )
        )

    response = await graphql(
        api_client,
        """
        query C($id: UUID!) {
          conversation(id: $id) { messages { citations { documentTitle } } }
        }
        """,
        {"id": str(conversation_id)},
        _auth(token),
    )

    messages = response.json()["data"]["conversation"]["messages"]
    assert messages[0]["citations"] == []
    assert [c["documentTitle"] for c in messages[1]["citations"]] == ["Pricing"]
