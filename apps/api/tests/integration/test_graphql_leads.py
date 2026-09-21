"""`leads` -- the GraphQL read side of Task 8's dashboard.

Rows are seeded directly through `LeadService` rather than by driving
`create_lead` through a live chat turn, matching
`test_graphql_conversations.py`'s own reasoning: this is about what the read
surface returns, not about the tool that writes the rows.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.conversations.schemas import CreateConversationInput
from app.conversations.service import ConversationService
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel, MembershipRole
from app.leads.schemas import CreateLeadInput
from app.leads.service import LeadService
from app.main import create_app
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean(clean_users) -> None:
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
            "full_name": "Leads Owner",
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


async def _conversation(org_id: uuid.UUID, agent_id: uuid.UUID) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        conversation = await ConversationService(session, tenant).create(
            agent_id, CreateConversationInput(channel=ConversationChannel.PLAYGROUND)
        )
    return conversation.id


async def _lead(
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    name: str = "Jamie Rivera",
    email: str | None = "jamie@example.com",
    interest: str = "the pro plan",
) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        lead = await LeadService(session, tenant).create(
            agent_id,
            conversation_id,
            CreateLeadInput(name=name, email=email, phone=None, interest=interest),
        )
    return lead.id


LEADS_QUERY = """
query Leads($agentId: UUID!) {
  leads(agentId: $agentId) {
    id name email phone interest status createdAt
    conversation { id title }
  }
}
"""


async def test_leads_lists_an_agents_captured_leads(api_client):
    token = await _register(api_client, "list-leads@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)
    lead_id = await _lead(org_id, agent_id, conversation_id)

    response = await graphql(api_client, LEADS_QUERY, {"agentId": str(agent_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    rows = body["data"]["leads"]
    assert [row["id"] for row in rows] == [str(lead_id)]
    assert rows[0]["name"] == "Jamie Rivera"
    assert rows[0]["email"] == "jamie@example.com"
    assert rows[0]["interest"] == "the pro plan"
    # LeadStatus.NEW is the column's own default -- nothing set it explicitly.
    assert rows[0]["status"] == "NEW"
    assert rows[0]["conversation"]["id"] == str(conversation_id)


async def test_leads_are_most_recently_captured_first(api_client):
    token = await _register(api_client, "order-leads@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)
    first_id = await _lead(org_id, agent_id, conversation_id, name="First Lead")
    second_id = await _lead(org_id, agent_id, conversation_id, name="Second Lead")

    response = await graphql(api_client, LEADS_QUERY, {"agentId": str(agent_id)}, _auth(token))

    rows = response.json()["data"]["leads"]
    assert [row["id"] for row in rows] == [str(second_id), str(first_id)]


async def test_leads_for_another_organizations_agent_is_empty(api_client):
    """Not an error, matching `conversations`: an agent id that is not yours
    is indistinguishable from one that does not exist."""
    owner_token = await _register(api_client, "leads-owner-a@example.com", "Leads Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)
    conversation_id = await _conversation(owner_org_id, agent_id)
    await _lead(owner_org_id, agent_id, conversation_id)

    other_token = await _register(api_client, "leads-owner-b@example.com", "Leads Org B")

    response = await graphql(
        api_client, LEADS_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["leads"] == []


async def test_leads_requires_authentication(api_client):
    response = await graphql(api_client, LEADS_QUERY, {"agentId": str(uuid.uuid4())})

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_a_lead_with_no_name_or_email_still_renders(api_client):
    """`name`/`email`/`phone`/`interest` are all nullable on the model -- a
    partial capture (phone only) is still a real row, and the query must not
    choke on the missing fields."""
    token = await _register(api_client, "partial-lead@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    conversation_id = await _conversation(org_id, agent_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        await LeadService(session, tenant).create(
            agent_id,
            conversation_id,
            CreateLeadInput(
                name="Phone Only", email=None, phone="+15551234567", interest="pricing"
            ),
        )

    response = await graphql(api_client, LEADS_QUERY, {"agentId": str(agent_id)}, _auth(token))

    rows = response.json()["data"]["leads"]
    assert rows[0]["email"] is None
    assert rows[0]["phone"] == "+15551234567"
