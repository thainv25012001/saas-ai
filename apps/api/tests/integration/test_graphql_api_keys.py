"""The GraphQL surface for API keys and an agent's MCP info (Task 4,
docs/PHASE-7.md §6): `apiKeys`/`createApiKey`/`revokeApiKey`, thin wrappers
over `ApiKeyService` (Task 2), and `agentMcpInfo`, which reports an agent's
granted tools intersected with `MCP_EXPOSED_TOOL_NAMES` (Task 3) -- the exact
set an MCP client would see from `tools/list`.

Follows `test_graphql_agent_tools.py`'s idiom: a real ASGI client, real
registration, rows created either through the mutation under test or
directly through the service.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.api_keys.service import ApiKeyService
from app.core.ids import uuid7
from app.core.security import create_access_token
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import MembershipRole
from app.main import create_app
from app.mcp.server import MCP_EXPOSED_TOOL_NAMES
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
            "full_name": "Keys Owner",
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


async def _user_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["user_id"])


def _tenant(org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=user_id, role=MembershipRole.OWNER, request_id="test"
    )


def _member_token(org_id: uuid.UUID) -> str:
    """A token for a member of `org_id` with no owner/admin rights. The
    permission check (`ApiKeyService._require_privileged`) reads the role
    straight off the decoded token, not a database row, so no real
    `memberships` row is needed for this -- exactly like
    `test_api_key_service.py`'s own `_member` helper, one layer up."""
    return create_access_token(user_id=uuid7(), organization_id=org_id, role="member")


async def _agent(org_id: uuid.UUID, name: str = "Sales Bot") -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name))
    return agent.id


async def _create_key_directly(
    org_id: uuid.UUID, agent_id: uuid.UUID, name: str, user_id: uuid.UUID | None = None
) -> uuid.UUID:
    tenant = _tenant(org_id, user_id)
    async with tenant_session(tenant) as session:
        created = await ApiKeyService(session, tenant).create(agent_id, name)
    return created.api_key.id


API_KEYS_QUERY = """
query ApiKeys($agentId: UUID!) {
  apiKeys(agentId: $agentId) {
    id name keyPrefix agentId createdByName createdAt lastUsedAt revokedAt
  }
}
"""

CREATE_KEY_MUTATION = """
mutation CreateApiKey($agentId: UUID!, $name: String!) {
  createApiKey(agentId: $agentId, name: $name) {
    token
    apiKey { id name keyPrefix agentId createdAt lastUsedAt revokedAt }
  }
}
"""

REVOKE_KEY_MUTATION = """
mutation RevokeApiKey($id: UUID!) {
  revokeApiKey(id: $id) { id revokedAt }
}
"""

MCP_INFO_QUERY = """
query McpInfo($agentId: UUID!) {
  agentMcpInfo(agentId: $agentId) { exposedToolNames }
}
"""


# ---------------------------------------------------------------------------
# createApiKey
# ---------------------------------------------------------------------------


async def test_create_api_key_returns_a_token_once(api_client):
    token = await _register(api_client, "create-key@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    response = await graphql(
        api_client,
        CREATE_KEY_MUTATION,
        {"agentId": str(agent_id), "name": "MCP key"},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    created = body["data"]["createApiKey"]
    assert created["token"].startswith("sa_mcp_")
    assert created["apiKey"]["name"] == "MCP key"
    assert created["apiKey"]["keyPrefix"] == created["token"][:15]
    assert created["apiKey"]["agentId"] == str(agent_id)


async def test_api_key_type_has_no_token_field(api_client):
    """The plaintext token is reachable only through `createApiKey`'s own
    response type (`CreatedApiKey`) -- `ApiKey` itself carries no such field
    at the schema level, so asking for one on `apiKeys` is a validation
    error, not merely an omitted value."""
    token = await _register(api_client, "no-token-field@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    response = await graphql(
        api_client,
        "query Q($agentId: UUID!) { apiKeys(agentId: $agentId) { id token } }",
        {"agentId": str(agent_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" in body
    assert "token" in body["errors"][0]["message"]


async def test_create_api_key_for_a_cross_tenant_agent_is_not_found(api_client):
    owner_token = await _register(api_client, "create-cross-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)

    other_token = await _register(api_client, "create-cross-b@example.com", "Org B")

    response = await graphql(
        api_client,
        CREATE_KEY_MUTATION,
        {"agentId": str(agent_id), "name": "MCP key"},
        _auth(other_token),
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_member_role_cannot_create_a_key(api_client):
    owner_token = await _register(api_client, "member-create@example.com")
    org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(org_id)

    member_token = _member_token(org_id)
    response = await graphql(
        api_client,
        CREATE_KEY_MUTATION,
        {"agentId": str(agent_id), "name": "MCP key"},
        _auth(member_token),
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "forbidden"


async def test_create_api_key_requires_authentication(api_client):
    response = await graphql(
        api_client, CREATE_KEY_MUTATION, {"agentId": str(uuid.uuid4()), "name": "MCP key"}
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# apiKeys
# ---------------------------------------------------------------------------


async def test_api_keys_lists_newest_first_without_a_token(api_client):
    token = await _register(api_client, "list-keys@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    await _create_key_directly(org_id, agent_id, "First")
    await _create_key_directly(org_id, agent_id, "Second")

    response = await graphql(api_client, API_KEYS_QUERY, {"agentId": str(agent_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    rows = body["data"]["apiKeys"]
    assert [row["name"] for row in rows] == ["Second", "First"]
    for row in rows:
        assert "token" not in row


async def test_api_keys_for_a_cross_tenant_agent_is_not_found(api_client):
    owner_token = await _register(api_client, "list-cross-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)

    other_token = await _register(api_client, "list-cross-b@example.com", "Org B")

    response = await graphql(
        api_client, API_KEYS_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_api_keys_requires_authentication(api_client):
    response = await graphql(api_client, API_KEYS_QUERY, {"agentId": str(uuid.uuid4())})
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_api_keys_batches_created_by_name(api_client):
    """The N+1 `createdByName` dataloader exists to prevent -- follows
    `test_graphql_evaluations.py`'s statement-counting idiom."""
    from sqlalchemy import event

    from app.db.session import engine

    token = await _register(api_client, "batch-created-by@example.com")
    org_id = await _organization_id(api_client, token)
    user_id = await _user_id(api_client, token)
    agent_id = await _agent(org_id)
    for i in range(3):
        await _create_key_directly(org_id, agent_id, f"Key {i}", user_id)

    membership_statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        upper = statement.lstrip().upper()
        if upper.startswith("SELECT") and "memberships" in statement:
            membership_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await graphql(
            api_client, API_KEYS_QUERY, {"agentId": str(agent_id)}, _auth(token)
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    body = response.json()
    assert "errors" not in body, body
    rows = body["data"]["apiKeys"]
    assert len(rows) == 3
    for row in rows:
        assert row["createdByName"] == "Keys Owner"
    assert len(membership_statements) == 1, (
        f"expected one batched query, got {len(membership_statements)}"
    )


async def test_created_by_name_is_null_after_the_creator_is_deleted(api_client):
    """`api_keys.created_by` is `ON DELETE SET NULL` -- a deleted creator
    must not surface as a broken lookup, just an absent name."""
    from sqlalchemy import text as sql_text

    token = await _register(api_client, "creator-deleted@example.com")
    org_id = await _organization_id(api_client, token)
    user_id = await _user_id(api_client, token)
    agent_id = await _agent(org_id)
    await _create_key_directly(org_id, agent_id, "MCP key", user_id)

    async with untenanted_session() as session:
        await session.execute(sql_text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    response = await graphql(api_client, API_KEYS_QUERY, {"agentId": str(agent_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["apiKeys"]
    assert row["createdByName"] is None


# ---------------------------------------------------------------------------
# revokeApiKey
# ---------------------------------------------------------------------------


async def test_revoke_api_key_is_idempotent(api_client):
    token = await _register(api_client, "revoke-key@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)
    key_id = await _create_key_directly(org_id, agent_id, "MCP key")

    first = await graphql(api_client, REVOKE_KEY_MUTATION, {"id": str(key_id)}, _auth(token))
    second = await graphql(api_client, REVOKE_KEY_MUTATION, {"id": str(key_id)}, _auth(token))

    first_body, second_body = first.json(), second.json()
    assert "errors" not in first_body, first_body
    assert "errors" not in second_body, second_body
    assert first_body["data"]["revokeApiKey"]["revokedAt"] is not None
    assert (
        first_body["data"]["revokeApiKey"]["revokedAt"]
        == (second_body["data"]["revokeApiKey"]["revokedAt"])
    )


async def test_revoke_api_key_for_a_cross_tenant_key_is_not_found(api_client):
    owner_token = await _register(api_client, "revoke-cross-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)
    key_id = await _create_key_directly(owner_org_id, agent_id, "MCP key")

    other_token = await _register(api_client, "revoke-cross-b@example.com", "Org B")

    response = await graphql(
        api_client, REVOKE_KEY_MUTATION, {"id": str(key_id)}, _auth(other_token)
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_member_role_cannot_revoke_a_key(api_client):
    owner_token = await _register(api_client, "member-revoke@example.com")
    org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(org_id)
    key_id = await _create_key_directly(org_id, agent_id, "MCP key")

    member_token = _member_token(org_id)
    response = await graphql(
        api_client, REVOKE_KEY_MUTATION, {"id": str(key_id)}, _auth(member_token)
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "forbidden"


async def test_revoke_api_key_requires_authentication(api_client):
    response = await graphql(api_client, REVOKE_KEY_MUTATION, {"id": str(uuid.uuid4())})
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# agentMcpInfo
# ---------------------------------------------------------------------------


async def test_agent_mcp_info_reflects_default_grants(api_client):
    """A freshly created agent has `retrieve_knowledge`/`search_products`/
    `get_product` granted by default (Task 7b/5) -- all three are also in
    `MCP_EXPOSED_TOOL_NAMES`, so all three show up here."""
    token = await _register(api_client, "mcp-info-default@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    response = await graphql(api_client, MCP_INFO_QUERY, {"agentId": str(agent_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    names = set(body["data"]["agentMcpInfo"]["exposedToolNames"])
    assert names == {"search_products", "get_product", "retrieve_knowledge"}
    assert names <= MCP_EXPOSED_TOOL_NAMES


async def test_agent_mcp_info_never_lists_create_lead_even_when_granted(api_client):
    """`create_lead` is grantable (Task 8's toggle) but not in
    `MCP_EXPOSED_TOOL_NAMES` at all -- an MCP client has no conversation to
    attach a lead to (docs/PHASE-7.md §5). Granting it must not leak it into
    this list."""
    token = await _register(api_client, "mcp-info-create-lead@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    tools = await graphql(
        api_client,
        "query T($agentId: UUID!) { agentTools(agentId: $agentId) { id name } }",
        {"agentId": str(agent_id)},
        _auth(token),
    )
    create_lead_id = next(
        row["id"] for row in tools.json()["data"]["agentTools"] if row["name"] == "create_lead"
    )
    enabled = await graphql(
        api_client,
        """
        mutation Set($agentId: UUID!, $toolId: UUID!, $isEnabled: Boolean!) {
          setAgentToolEnabled(agentId: $agentId, toolId: $toolId, isEnabled: $isEnabled) { id }
        }
        """,
        {"agentId": str(agent_id), "toolId": create_lead_id, "isEnabled": True},
        _auth(token),
    )
    assert "errors" not in enabled.json(), enabled.json()

    response = await graphql(api_client, MCP_INFO_QUERY, {"agentId": str(agent_id)}, _auth(token))
    names = response.json()["data"]["agentMcpInfo"]["exposedToolNames"]
    assert "create_lead" not in names


async def test_agent_mcp_info_reflects_a_tool_being_disabled(api_client):
    token = await _register(api_client, "mcp-info-disable@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id = await _agent(org_id)

    tools = await graphql(
        api_client,
        "query T($agentId: UUID!) { agentTools(agentId: $agentId) { id name } }",
        {"agentId": str(agent_id)},
        _auth(token),
    )
    search_id = next(
        row["id"] for row in tools.json()["data"]["agentTools"] if row["name"] == "search_products"
    )
    await graphql(
        api_client,
        """
        mutation Set($agentId: UUID!, $toolId: UUID!, $isEnabled: Boolean!) {
          setAgentToolEnabled(agentId: $agentId, toolId: $toolId, isEnabled: $isEnabled) { id }
        }
        """,
        {"agentId": str(agent_id), "toolId": search_id, "isEnabled": False},
        _auth(token),
    )

    response = await graphql(api_client, MCP_INFO_QUERY, {"agentId": str(agent_id)}, _auth(token))
    names = response.json()["data"]["agentMcpInfo"]["exposedToolNames"]
    assert "search_products" not in names


async def test_agent_mcp_info_for_another_organizations_agent_is_not_found(api_client):
    """Ownership check first -- unlike `agentTools`/`leads`, a cross-tenant
    `agentId` here must be a GraphQL error, not an empty-shaped result."""
    owner_token = await _register(api_client, "mcp-info-cross-a@example.com", "Org A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id = await _agent(owner_org_id)

    other_token = await _register(api_client, "mcp-info-cross-b@example.com", "Org B")

    response = await graphql(
        api_client, MCP_INFO_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_agent_mcp_info_requires_authentication(api_client):
    response = await graphql(api_client, MCP_INFO_QUERY, {"agentId": str(uuid.uuid4())})
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"
