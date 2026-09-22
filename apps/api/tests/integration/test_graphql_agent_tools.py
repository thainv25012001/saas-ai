"""`agentTools` and `setAgentToolEnabled` -- Task 8's dashboard surface for
turning a builtin tool on or off per agent.

Before this task, the only way to link `create_lead` to an agent was a raw
database write (`tests/conftest.py::enable_builtin_tool`) -- Task 7b shipped
it seeded and registrable, but reachable by no UI at all. These tests drive
the same read/write path the dashboard now uses.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
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
            "full_name": "Tools Owner",
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


AGENT_TOOLS_QUERY = """
query AgentTools($agentId: UUID!) {
  agentTools(agentId: $agentId) { id name description isEnabled }
}
"""

SET_TOOL_MUTATION = """
mutation SetAgentToolEnabled($agentId: UUID!, $toolId: UUID!, $isEnabled: Boolean!) {
  setAgentToolEnabled(agentId: $agentId, toolId: $toolId, isEnabled: $isEnabled) {
    id name isEnabled
  }
}
"""


async def test_a_new_agent_shows_retrieve_knowledge_enabled_and_create_lead_off(api_client):
    """Task 7b's own default: `retrieve_knowledge` is linked and enabled at
    creation, `create_lead` is seeded globally but linked to no agent -- this
    is the exact gap Task 8 closes with a toggle rather than a database
    write. Task 5 (Phase 5) adds `search_products`/`get_product` to the
    same default-on set as `retrieve_knowledge`, for the identical reason:
    both are reads with no risk `create_lead`'s write carries."""
    token = await _register(api_client, "new-agent-tools@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    response = await graphql(
        api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )

    body = response.json()
    assert "errors" not in body, body
    by_name = {row["name"]: row for row in body["data"]["agentTools"]}
    assert set(by_name) == {
        "retrieve_knowledge",
        "create_lead",
        "search_products",
        "get_product",
    }
    assert by_name["retrieve_knowledge"]["isEnabled"] is True
    assert by_name["search_products"]["isEnabled"] is True
    assert by_name["get_product"]["isEnabled"] is True
    assert by_name["create_lead"]["isEnabled"] is False


async def test_enabling_create_lead_makes_it_reachable(api_client):
    token = await _register(api_client, "enable-create-lead@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    listed = await graphql(api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(token))
    create_lead_id = next(
        row["id"] for row in listed.json()["data"]["agentTools"] if row["name"] == "create_lead"
    )

    mutated = await graphql(
        api_client,
        SET_TOOL_MUTATION,
        {"agentId": str(agent_id), "toolId": create_lead_id, "isEnabled": True},
        _auth(token),
    )
    body = mutated.json()
    assert "errors" not in body, body
    assert body["data"]["setAgentToolEnabled"]["isEnabled"] is True

    # The toggle actually persisted an `agent_tools` link, not just an
    # in-memory response -- a fresh query must agree.
    refetched = await graphql(
        api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )
    by_name = {row["name"]: row for row in refetched.json()["data"]["agentTools"]}
    assert by_name["create_lead"]["isEnabled"] is True


async def test_disabling_an_already_linked_tool_flips_the_existing_row(api_client):
    """`retrieve_knowledge` already has a link from agent creation -- this
    must update that row, not insert a conflicting second one."""
    token = await _register(api_client, "disable-retrieve@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    listed = await graphql(api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(token))
    retrieve_id = next(
        row["id"]
        for row in listed.json()["data"]["agentTools"]
        if row["name"] == "retrieve_knowledge"
    )

    mutated = await graphql(
        api_client,
        SET_TOOL_MUTATION,
        {"agentId": str(agent_id), "toolId": retrieve_id, "isEnabled": False},
        _auth(token),
    )
    assert mutated.json()["data"]["setAgentToolEnabled"]["isEnabled"] is False

    refetched = await graphql(
        api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )
    by_name = {row["name"]: row for row in refetched.json()["data"]["agentTools"]}
    assert by_name["retrieve_knowledge"]["isEnabled"] is False


async def test_another_organizations_agent_leaks_nothing_through_either_surface(api_client):
    """The tenant boundary, pinned on both halves of Task 8's surface.

    The QUERY returns an empty list (whole-branch review, Important 5 -- it
    used to raise `not_found`, disagreeing with `leads`, which was added in
    the same commit over the same argument). The MUTATION still raises:
    silently doing nothing to a write request is worse than saying it could
    not be done.
    """
    owner_token = await _register(api_client, "tools-owner-a@example.com", "Tools Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)

    other_token = await _register(api_client, "tools-owner-b@example.com", "Tools Org B")

    response = await graphql(
        api_client, AGENT_TOOLS_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )
    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["agentTools"] == []

    mutation = await graphql(
        api_client,
        SET_TOOL_MUTATION,
        {"agentId": str(agent_id), "toolId": str(uuid.uuid4()), "isEnabled": True},
        _auth(other_token),
    )
    mutation_body = mutation.json()
    assert mutation_body["errors"][0]["extensions"]["code"] == "not_found"


async def test_set_agent_tool_enabled_requires_authentication(api_client):
    response = await graphql(
        api_client,
        SET_TOOL_MUTATION,
        {"agentId": str(uuid.uuid4()), "toolId": str(uuid.uuid4()), "isEnabled": True},
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


LEADS_QUERY = """
query Leads($agentId: UUID!) {
  leads(agentId: $agentId) { id name }
}
"""


async def test_a_foreign_agent_id_gives_both_new_queries_the_same_empty_answer(api_client):
    """Whole-branch review, Important 5. `leads` and `agentTools` were added
    in the same commit, take the same `agentId`, and are rendered on adjacent
    pages -- and disagreed: one returned an empty list for another
    organization's agent, the other a GraphQL error. Silent-empty is the
    convention `conversations` and `documents` already follow, so that is the
    one both now use. Asserted against a REAL second organization's agent id,
    not a random UUID, so the test covers "exists but is not yours" and not
    only "does not exist".
    """
    token_a = await _register(api_client, "coherence-a@example.com")
    token_b = await _register(api_client, "coherence-b@example.com", org_name="Ada Motors B")
    org_b = await _organization_id(api_client, token_b)
    foreign_agent_id = await _agent(org_b)

    tools = await graphql(
        api_client, AGENT_TOOLS_QUERY, {"agentId": str(foreign_agent_id)}, _auth(token_a)
    )
    leads = await graphql(
        api_client, LEADS_QUERY, {"agentId": str(foreign_agent_id)}, _auth(token_a)
    )

    tools_body, leads_body = tools.json(), leads.json()
    assert "errors" not in tools_body, tools_body
    assert "errors" not in leads_body, leads_body
    assert tools_body["data"]["agentTools"] == []
    assert leads_body["data"]["leads"] == []
