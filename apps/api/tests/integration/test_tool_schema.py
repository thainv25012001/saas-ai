"""Schema for tools, tool calls and leads (Phase 4 Task 3).

`tools.organization_id` is the one nullable tenant column in this schema --
NULL means a global builtin visible to every organization -- so its RLS
policy cannot be the plain equality every other table gets from
`enable_rls`. The tests here assert both halves of that: a global builtin is
visible to two *different* organizations, and an org-scoped tool is visible
to exactly one. Asserting only the first half would also pass against a
policy with no tenant predicate at all (`USING (true)`), which is exactly
the vacuous shape this project keeps catching -- see the module docstring in
tests/integration/test_isolation_layers.py.

Every other new table (agent_tools, message_tool_calls, leads) gets the
ordinary single-org policy, proven the same way as agents/documents in
test_isolation_layers.py: raw SQL through `tenant_session`, no service layer
in the picture, so RLS is the only thing keeping another tenant's row out.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session

pytestmark = pytest.mark.anyio


@pytest.fixture
async def tools_global_and_scoped(owner_connection, tenant_a, tenant_b):
    """One global builtin (organization_id NULL) plus one tool scoped to
    tenant A. Inserted as app_owner, bypassing RLS entirely, matching the
    fixture style in test_isolation_layers.py."""
    builtin_id, scoped_id = uuid7(), uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, NULL, 'retrieve_knowledge', 'builtin', '{}', true)"
        ),
        {"id": builtin_id},
    )
    await owner_connection.execute(
        text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, :org, 'private_crm_lookup', 'http', '{}', true)"
        ),
        {"id": scoped_id, "org": tenant_a.organization_id},
    )
    await owner_connection.commit()
    yield builtin_id, scoped_id
    await owner_connection.execute(
        text("DELETE FROM tools WHERE id IN (:a, :b)"), {"a": builtin_id, "b": scoped_id}
    )
    await owner_connection.commit()


async def _visible_tool_ids(tenant: TenantContext) -> set[uuid.UUID]:
    async with tenant_session(tenant) as session:
        rows = await session.execute(text("SELECT id FROM tools"))
        return {row.id for row in rows}


async def test_global_builtin_tool_visible_to_two_different_orgs(
    tools_global_and_scoped, tenant_a: TenantContext, tenant_b: TenantContext
):
    builtin_id, _scoped_id = tools_global_and_scoped
    assert tenant_a.organization_id != tenant_b.organization_id

    visible_to_a = await _visible_tool_ids(tenant_a)
    visible_to_b = await _visible_tool_ids(tenant_b)

    assert builtin_id in visible_to_a
    assert builtin_id in visible_to_b


async def test_org_scoped_tool_visible_only_to_its_own_org(
    tools_global_and_scoped, tenant_a: TenantContext, tenant_b: TenantContext
):
    _builtin_id, scoped_id = tools_global_and_scoped

    visible_to_a = await _visible_tool_ids(tenant_a)
    visible_to_b = await _visible_tool_ids(tenant_b)

    # Positive control: an isolation bug that hid everything (including the
    # owner's own row) would otherwise satisfy the negative assertion below
    # for free.
    assert scoped_id in visible_to_a
    assert scoped_id not in visible_to_b


@pytest.fixture
async def agents_and_agent_tools(owner_connection, tenant_a, tenant_b):
    """An agent plus an enabled agent_tools link in each of two orgs, and one
    shared global tool both agents point at."""
    tool_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, NULL, 'retrieve_knowledge', 'builtin', '{}', true)"
        ),
        {"id": tool_id},
    )

    agent_a, agent_b = uuid7(), uuid7()
    for agent_id, tenant, name in (
        (agent_a, tenant_a, "Tool Link Probe A"),
        (agent_b, tenant_b, "Tool Link Probe B"),
    ):
        await owner_connection.execute(
            text(
                "INSERT INTO agents "
                "(id, organization_id, name, slug, status, provider, model, "
                " temperature, max_tokens, public_key) "
                "VALUES (:id, :org, :name, :slug, 'draft', 'openai', 'gpt-4o-mini', "
                " 0.3, 1024, :public_key)"
            ),
            {
                "id": agent_id,
                "org": tenant.organization_id,
                "name": name,
                "slug": name.lower().replace(" ", "-"),
                "public_key": f"pk_test_{agent_id.hex[:16]}",
            },
        )
        await owner_connection.execute(
            text(
                "INSERT INTO agent_tools "
                "(agent_id, tool_id, organization_id, is_enabled, overrides) "
                "VALUES (:agent_id, :tool_id, :org, true, '{}')"
            ),
            {"agent_id": agent_id, "tool_id": tool_id, "org": tenant.organization_id},
        )
    await owner_connection.commit()

    yield agent_a, agent_b, tool_id

    await owner_connection.execute(
        text("DELETE FROM agents WHERE id IN (:a, :b)"), {"a": agent_a, "b": agent_b}
    )
    await owner_connection.execute(text("DELETE FROM tools WHERE id = :id"), {"id": tool_id})
    await owner_connection.commit()


async def test_agent_tools_row_visible_only_to_its_own_org(
    agents_and_agent_tools, tenant_a: TenantContext, tenant_b: TenantContext
):
    agent_a, agent_b, _tool_id = agents_and_agent_tools

    async with tenant_session(tenant_a) as session:
        rows = await session.execute(text("SELECT agent_id FROM agent_tools"))
        visible_to_a = {row.agent_id for row in rows}
    async with tenant_session(tenant_b) as session:
        rows = await session.execute(text("SELECT agent_id FROM agent_tools"))
        visible_to_b = {row.agent_id for row in rows}

    assert agent_a in visible_to_a
    assert agent_b not in visible_to_a
    assert agent_b in visible_to_b
    assert agent_a not in visible_to_b


@pytest.fixture
async def messages_and_tool_calls(owner_connection, tenant_a, tenant_b):
    """A conversation, message and message_tool_calls row in each of two
    orgs."""
    conv_a, conv_b = uuid7(), uuid7()
    agent_a, agent_b = uuid7(), uuid7()
    msg_a, msg_b = uuid7(), uuid7()
    call_a, call_b = uuid7(), uuid7()

    for agent_id, conv_id, msg_id, call_id, tenant, name in (
        (agent_a, conv_a, msg_a, call_a, tenant_a, "Tool Call Probe A"),
        (agent_b, conv_b, msg_b, call_b, tenant_b, "Tool Call Probe B"),
    ):
        await owner_connection.execute(
            text(
                "INSERT INTO agents "
                "(id, organization_id, name, slug, status, provider, model, "
                " temperature, max_tokens, public_key) "
                "VALUES (:id, :org, :name, :slug, 'draft', 'openai', 'gpt-4o-mini', "
                " 0.3, 1024, :public_key)"
            ),
            {
                "id": agent_id,
                "org": tenant.organization_id,
                "name": name,
                "slug": name.lower().replace(" ", "-"),
                "public_key": f"pk_test_{agent_id.hex[:16]}",
            },
        )
        await owner_connection.execute(
            text(
                "INSERT INTO conversations (id, organization_id, agent_id, channel, status) "
                "VALUES (:id, :org, :agent_id, 'api', 'open')"
            ),
            {"id": conv_id, "org": tenant.organization_id, "agent_id": agent_id},
        )
        await owner_connection.execute(
            text(
                "INSERT INTO messages (id, organization_id, conversation_id, seq, role, content) "
                "VALUES (:id, :org, :conv_id, 1, 'assistant', 'hello')"
            ),
            {"id": msg_id, "org": tenant.organization_id, "conv_id": conv_id},
        )
        await owner_connection.execute(
            text(
                "INSERT INTO message_tool_calls "
                "(id, organization_id, message_id, tool_call_id, tool_name, arguments, "
                " result, is_error) "
                "VALUES (:id, :org, :msg_id, :tool_call_id, 'retrieve_knowledge', '{}', "
                " '{}', false)"
            ),
            {
                "id": call_id,
                "org": tenant.organization_id,
                "msg_id": msg_id,
                "tool_call_id": f"call_{call_id.hex[:8]}",
            },
        )
    await owner_connection.commit()

    yield call_a, call_b

    await owner_connection.execute(
        text("DELETE FROM agents WHERE id IN (:a, :b)"), {"a": agent_a, "b": agent_b}
    )
    await owner_connection.commit()


async def test_message_tool_calls_row_visible_only_to_its_own_org(
    messages_and_tool_calls, tenant_a: TenantContext, tenant_b: TenantContext
):
    call_a, call_b = messages_and_tool_calls

    async with tenant_session(tenant_a) as session:
        rows = await session.execute(text("SELECT id FROM message_tool_calls"))
        visible_to_a = {row.id for row in rows}
    async with tenant_session(tenant_b) as session:
        rows = await session.execute(text("SELECT id FROM message_tool_calls"))
        visible_to_b = {row.id for row in rows}

    assert call_a in visible_to_a
    assert call_b not in visible_to_a
    assert call_b in visible_to_b
    assert call_a not in visible_to_b


@pytest.fixture
async def leads_in_two_orgs(owner_connection, tenant_a, tenant_b):
    lead_a, lead_b = uuid7(), uuid7()
    agent_a, agent_b = uuid7(), uuid7()
    conv_a, conv_b = uuid7(), uuid7()

    for agent_id, conv_id, lead_id, tenant, name in (
        (agent_a, conv_a, lead_a, tenant_a, "Lead Probe A"),
        (agent_b, conv_b, lead_b, tenant_b, "Lead Probe B"),
    ):
        await owner_connection.execute(
            text(
                "INSERT INTO agents "
                "(id, organization_id, name, slug, status, provider, model, "
                " temperature, max_tokens, public_key) "
                "VALUES (:id, :org, :name, :slug, 'draft', 'openai', 'gpt-4o-mini', "
                " 0.3, 1024, :public_key)"
            ),
            {
                "id": agent_id,
                "org": tenant.organization_id,
                "name": name,
                "slug": name.lower().replace(" ", "-"),
                "public_key": f"pk_test_{agent_id.hex[:16]}",
            },
        )
        await owner_connection.execute(
            text(
                "INSERT INTO conversations (id, organization_id, agent_id, channel, status) "
                "VALUES (:id, :org, :agent_id, 'widget', 'open')"
            ),
            {"id": conv_id, "org": tenant.organization_id, "agent_id": agent_id},
        )
        await owner_connection.execute(
            text(
                "INSERT INTO leads "
                "(id, organization_id, agent_id, conversation_id, name, email, status) "
                "VALUES (:id, :org, :agent_id, :conv_id, :name, 'lead@example.com', 'new')"
            ),
            {
                "id": lead_id,
                "org": tenant.organization_id,
                "agent_id": agent_id,
                "conv_id": conv_id,
                "name": name,
            },
        )
    await owner_connection.commit()

    yield lead_a, lead_b

    await owner_connection.execute(
        text("DELETE FROM agents WHERE id IN (:a, :b)"), {"a": agent_a, "b": agent_b}
    )
    await owner_connection.commit()


async def test_leads_row_visible_only_to_its_own_org(
    leads_in_two_orgs, tenant_a: TenantContext, tenant_b: TenantContext
):
    lead_a, lead_b = leads_in_two_orgs

    async with tenant_session(tenant_a) as session:
        rows = await session.execute(text("SELECT id FROM leads"))
        visible_to_a = {row.id for row in rows}
    async with tenant_session(tenant_b) as session:
        rows = await session.execute(text("SELECT id FROM leads"))
        visible_to_b = {row.id for row in rows}

    assert lead_a in visible_to_a
    assert lead_b not in visible_to_a
    assert lead_b in visible_to_b
    assert lead_a not in visible_to_b


async def test_lead_status_defaults_to_new(owner_connection, tenant_a: TenantContext):
    """Exercises the ORM model end to end (not just raw SQL against the
    migration): Lead/LeadStatus round-trip through a real session, and the
    column default matches the enum's NEW member."""
    from app.core.tenancy import tenant_session
    from app.db.models.agent import Agent
    from app.db.models.conversation import Conversation, ConversationChannel
    from app.db.models.lead import Lead, LeadStatus

    agent = Agent(
        organization_id=tenant_a.organization_id,
        name="ORM Lead Probe",
        slug="orm-lead-probe",
        provider="openai",
        model="gpt-4o-mini",
        public_key=f"pk_test_{uuid7().hex[:16]}",
    )
    async with tenant_session(tenant_a) as session:
        session.add(agent)
        await session.flush()
        conversation = Conversation(
            organization_id=tenant_a.organization_id,
            agent_id=agent.id,
            channel=ConversationChannel.WIDGET,
        )
        session.add(conversation)
        await session.flush()
        lead = Lead(
            organization_id=tenant_a.organization_id,
            agent_id=agent.id,
            conversation_id=conversation.id,
            email="orm-lead@example.com",
        )
        session.add(lead)
        await session.flush()
        assert lead.status == LeadStatus.NEW
        lead_id = lead.id

    await owner_connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent.id})
    await owner_connection.commit()
    assert lead_id is not None
