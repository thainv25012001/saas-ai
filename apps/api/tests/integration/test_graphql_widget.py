"""The GraphQL surface for widget settings (Task 4,
docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §7): a thin
wrapper over `WidgetSettingsService` (Task 1), plus `Agent.publicKey` and
`Lead.source`, both plain passthroughs of columns Task 1/Task 2 already
populate.

Follows `test_graphql_api_keys.py`'s idiom: a real ASGI client, real
registration, rows created either through the mutation under test or
directly through the service.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.conversations.schemas import CreateConversationInput
from app.conversations.service import ConversationService
from app.core.ids import uuid7
from app.core.security import create_access_token
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel, MembershipRole
from app.leads.schemas import CreateLeadInput
from app.leads.service import LeadService
from app.main import create_app
from app.widget.schemas import UpdateWidgetSettingsInput
from app.widget.service import WidgetSettingsService
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


async def _register(api_client: AsyncClient, email: str, org_name: str = "Widget Motors") -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Widget Owner",
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


def _tenant(org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=user_id, role=MembershipRole.OWNER, request_id="test"
    )


def _member_token(org_id: uuid.UUID) -> str:
    """Same trick as `test_graphql_api_keys.py::_member_token`: the
    permission check reads the role straight off the decoded token, never a
    database row, so no real `memberships` row is needed for this."""
    return create_access_token(user_id=uuid7(), organization_id=org_id, role="member")


async def _agent(org_id: uuid.UUID, name: str = "Sales Bot") -> tuple[uuid.UUID, str]:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name))
    return agent.id, agent.public_key


async def _valid_settings_update(**overrides: object) -> UpdateWidgetSettingsInput:
    fields: dict[str, object] = {
        "enabled": True,
        "allowed_origins": ["https://shop.example.com"],
        "brand_color": "#123abc",
        "position": "right",
        "title": "Chat with us",
        "daily_message_cap": 250,
        **overrides,
    }
    return UpdateWidgetSettingsInput(**fields)  # type: ignore[arg-type]


async def _update_settings_directly(
    org_id: uuid.UUID, agent_id: uuid.UUID, **overrides: object
) -> None:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        await WidgetSettingsService(session, tenant).update(
            agent_id, await _valid_settings_update(**overrides)
        )


async def _lead_with_source(org_id: uuid.UUID, agent_id: uuid.UUID, source: str) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        conversation = await ConversationService(session, tenant).create(
            agent_id,
            CreateConversationInput(channel=ConversationChannel(source), visitor_id=None),
        )
        lead = await LeadService(session, tenant).create(
            agent_id,
            conversation.id,
            CreateLeadInput(
                name="Ada Lovelace",
                email="ada@example.com",
                phone=None,
                interest="pricing",
            ),
        )
    return lead.id


WIDGET_SETTINGS_QUERY = """
query WidgetSettings($agentId: UUID!) {
  widgetSettings(agentId: $agentId) {
    agentId enabled allowedOrigins brandColor position title dailyMessageCap
  }
}
"""

UPDATE_WIDGET_SETTINGS_MUTATION = """
mutation UpdateWidgetSettings($agentId: UUID!, $input: UpdateWidgetSettingsInput!) {
  updateWidgetSettings(agentId: $agentId, input: $input) {
    agentId enabled allowedOrigins brandColor position title dailyMessageCap
  }
}
"""


def _input(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "enabled": True,
        "allowedOrigins": ["https://shop.example.com"],
        "brandColor": "#123abc",
        "position": "RIGHT",
        "title": "Chat with us",
        "dailyMessageCap": 250,
        **overrides,
    }
    return fields


# ---------------------------------------------------------------------------
# widgetSettings
# ---------------------------------------------------------------------------


async def test_widget_settings_defaults_for_a_fresh_agent(api_client):
    token = await _register(api_client, "widget-defaults@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)

    response = await graphql(
        api_client, WIDGET_SETTINGS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )

    body = response.json()
    assert "errors" not in body, body
    settings = body["data"]["widgetSettings"]
    assert settings == {
        "agentId": str(agent_id),
        "enabled": False,
        "allowedOrigins": [],
        "brandColor": "#2563eb",
        "position": "RIGHT",
        "title": None,
        "dailyMessageCap": 500,
    }


async def test_widget_settings_for_a_cross_tenant_agent_is_not_found(api_client):
    owner_token = await _register(api_client, "widget-cross-a@example.com", "Widget Motors A")
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id, _ = await _agent(owner_org_id)

    other_token = await _register(api_client, "widget-cross-b@example.com", "Widget Motors B")

    response = await graphql(
        api_client, WIDGET_SETTINGS_QUERY, {"agentId": str(agent_id)}, _auth(other_token)
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_widget_settings_requires_authentication(api_client):
    response = await graphql(api_client, WIDGET_SETTINGS_QUERY, {"agentId": str(uuid.uuid4())})
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# updateWidgetSettings
# ---------------------------------------------------------------------------


async def test_update_widget_settings_round_trips_and_normalizes(api_client):
    token = await _register(api_client, "widget-update@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)

    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {
            "agentId": str(agent_id),
            "input": _input(allowedOrigins=["https://Shop.Example.com/"]),
        },
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    settings = body["data"]["updateWidgetSettings"]
    assert settings["allowedOrigins"] == ["https://shop.example.com"]
    assert settings["enabled"] is True
    assert settings["brandColor"] == "#123abc"
    assert settings["position"] == "RIGHT"
    assert settings["title"] == "Chat with us"
    assert settings["dailyMessageCap"] == 250

    # Round-trips through a fresh read too.
    read = await graphql(
        api_client, WIDGET_SETTINGS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )
    assert read.json()["data"]["widgetSettings"]["allowedOrigins"] == ["https://shop.example.com"]


async def test_update_widget_settings_left_position(api_client):
    token = await _register(api_client, "widget-left@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)

    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {"agentId": str(agent_id), "input": _input(position="LEFT")},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["updateWidgetSettings"]["position"] == "LEFT"


async def test_update_widget_settings_rejects_an_invalid_origin_naming_it(api_client):
    token = await _register(api_client, "widget-invalid-origin@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)

    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {
            "agentId": str(agent_id),
            "input": _input(allowedOrigins=["https://a.com/path"]),
        },
        _auth(token),
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"
    assert repr("https://a.com/path") in body["errors"][0]["message"]

    # Nothing was stored -- a fresh read still shows the defaults.
    read = await graphql(
        api_client, WIDGET_SETTINGS_QUERY, {"agentId": str(agent_id)}, _auth(token)
    )
    assert read.json()["data"]["widgetSettings"]["allowedOrigins"] == []


async def test_member_role_cannot_update_widget_settings(api_client):
    owner_token = await _register(api_client, "widget-member@example.com")
    org_id = await _organization_id(api_client, owner_token)
    agent_id, _ = await _agent(org_id)

    member_token = _member_token(org_id)
    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {"agentId": str(agent_id), "input": _input()},
        _auth(member_token),
    )

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "forbidden"


async def test_update_widget_settings_for_a_cross_tenant_agent_is_not_found(api_client):
    owner_token = await _register(
        api_client, "widget-update-cross-a@example.com", "Widget Motors A"
    )
    owner_org_id = await _organization_id(api_client, owner_token)
    agent_id, _ = await _agent(owner_org_id)

    other_token = await _register(
        api_client, "widget-update-cross-b@example.com", "Widget Motors B"
    )

    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {"agentId": str(agent_id), "input": _input()},
        _auth(other_token),
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"


async def test_update_widget_settings_requires_authentication(api_client):
    response = await graphql(
        api_client,
        UPDATE_WIDGET_SETTINGS_MUTATION,
        {"agentId": str(uuid.uuid4()), "input": _input()},
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Agent.publicKey
# ---------------------------------------------------------------------------


async def test_agent_public_key_starts_with_pk(api_client):
    token = await _register(api_client, "widget-public-key@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, public_key = await _agent(org_id)

    response = await graphql(
        api_client,
        "query A($id: UUID!) { agent(id: $id) { id publicKey } }",
        {"id": str(agent_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["agent"]["publicKey"] == public_key
    assert body["data"]["agent"]["publicKey"].startswith("pk_")


async def test_agent_public_key_requires_authentication(api_client):
    response = await graphql(
        api_client,
        "query A($id: UUID!) { agent(id: $id) { id publicKey } }",
        {"id": str(uuid.uuid4())},
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Lead.source
# ---------------------------------------------------------------------------


async def test_lead_source_returns_the_stored_value(api_client):
    token = await _register(api_client, "widget-lead-source@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)
    lead_id = await _lead_with_source(org_id, agent_id, "widget")

    response = await graphql(
        api_client,
        "query L($agentId: UUID!) { leads(agentId: $agentId) { id source } }",
        {"agentId": str(agent_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["leads"]
    assert row["id"] == str(lead_id)
    assert row["source"] == "widget"


async def test_lead_source_is_null_for_a_lead_created_before_source_was_tracked(api_client):
    """`Lead.source` is nullable specifically so a lead written before Task 2
    set it (`source IS NULL` in the database) still reads back cleanly here,
    rather than needing a backfill -- constructed with a direct ORM insert to
    simulate exactly that pre-existing row, since `LeadService.create` itself
    always sets `source` now."""
    from app.db.models import Lead as LeadModel

    token = await _register(api_client, "widget-lead-source-null@example.com")
    org_id = await _organization_id(api_client, token)
    agent_id, _ = await _agent(org_id)

    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        conversation = await ConversationService(session, tenant).create(
            agent_id,
            CreateConversationInput(channel=ConversationChannel.PLAYGROUND, visitor_id=None),
        )
        lead = LeadModel(
            id=uuid7(),
            organization_id=org_id,
            agent_id=agent_id,
            conversation_id=conversation.id,
            name="Pre-existing Lead",
            email="pre-existing@example.com",
            phone=None,
            interest="legacy",
            source=None,
        )
        session.add(lead)
        await session.flush()

    response = await graphql(
        api_client,
        "query L($agentId: UUID!) { leads(agentId: $agentId) { source } }",
        {"agentId": str(agent_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["leads"]
    assert row["source"] is None
