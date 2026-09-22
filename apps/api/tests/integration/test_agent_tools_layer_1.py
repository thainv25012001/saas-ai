"""Layer 1 on the tool-grant queries and the FK-bypass guard, isolated from RLS.

`docs/ARCHITECTURE.md` §2.3's two-layer tenancy: the explicit
`organization_id` predicate in the SQL (layer 1) and the RLS policy on the
session (layer 2). Every other test that touches these queries runs on an
ordinary RLS-scoped session, where layer 2 alone already stops a leak --
which cannot tell layer 1 apart from layer 1 not existing at all. The
whole-branch review proved that emptily: deleting the one predicate that
existed left 42 tests green, and no-op'ing
`ChatService._assert_message_belongs_to_tenant` left 52 green.

Every test here therefore runs the query on an `app_owner` session -- the
migration role that BYPASSES RLS -- with no `app.current_org_id` set, the
same technique `test_retrieve.py::
test_organization_id_predicate_holds_even_when_rls_is_bypassed` uses, and for
the reason it explains there: a merely unscoped (non-bypassing) session fails
closed under RLS's own `NULLIF(current_setting(...), '')` guard and would
pass with the predicate deleted.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.service import AgentService
from app.chat.service import ChatService
from app.conversations.schemas import AppendMessageInput, CreateConversationInput
from app.conversations.service import ConversationService
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import tenant_session
from app.db.models import ConversationChannel, MessageRole
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


@pytest.fixture
async def rls_bypassing_session() -> AsyncIterator[AsyncSession]:
    """An `AsyncSession` as `app_owner`: RLS does not apply to it at all, and
    `app.current_org_id` is never set on it. Layer 2 is therefore absent, and
    anything that still refuses to cross a tenant boundary on it is layer 1
    doing it."""
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def agent_with_a_foreign_labelled_link(owner_connection, tenant_a, tenant_b):
    """An agent that genuinely belongs to org A, plus an `agent_tools` row for
    it that is labelled with org B's `organization_id`.

    That row is the anomaly layer 1 exists to refuse: `agent_tools`
    denormalizes `organization_id`, so nothing in the schema forces it to
    agree with the agent's own. Inserted as `app_owner`, the way
    `test_tool_schema.py`'s fixtures insert their probes, because no
    application path can create it -- which is the point. `create_lead` is the
    tool used because Task 7b deliberately links it to no agent by default, so
    a resolver reporting it as available can only have got there through this
    row.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input("Layer One Bot"))
        agent_id = agent.id
    tool_row = await owner_connection.execute(
        text("SELECT id FROM tools WHERE name = 'create_lead' AND organization_id IS NULL")
    )
    tool_id: uuid.UUID = tool_row.scalar_one()
    await owner_connection.execute(
        text(
            "INSERT INTO agent_tools (agent_id, tool_id, organization_id, is_enabled, overrides) "
            "VALUES (:agent, :tool, :org, true, '{}')"
        ),
        {"agent": agent_id, "tool": tool_id, "org": tenant_b.organization_id},
    )
    await owner_connection.commit()
    yield agent_id, tool_id
    await owner_connection.execute(
        text("DELETE FROM agent_tools WHERE agent_id = :agent"), {"agent": agent_id}
    )
    await owner_connection.commit()


async def test_resolve_enabled_tool_names_ignores_a_foreign_labelled_link(
    rls_bypassing_session, tenant_a, agent_with_a_foreign_labelled_link
):
    """`ChatService._resolve_enabled_tool_names` decides what the agent may
    call -- and, since the whole-branch review's Critical 1 fix, what the tool
    registry even contains, so this predicate is now load-bearing for
    execution and not only for what the model is shown. It was the one
    Layer-1 filter in this area that existed, and it was itself unpinned."""
    agent_id, _ = agent_with_a_foreign_labelled_link
    names = await ChatService(rls_bypassing_session, tenant_a)._resolve_enabled_tool_names(agent_id)
    assert "create_lead" not in names


async def test_list_tools_ignores_a_foreign_labelled_link(
    rls_bypassing_session, tenant_a, agent_with_a_foreign_labelled_link
):
    """The dashboard's read half. Without layer 1 in the LEFT JOIN's ON
    clause, org B's link row makes the toggle render `create_lead` as already
    on for org A's agent."""
    agent_id, _ = agent_with_a_foreign_labelled_link
    pairs = await AgentService(rls_bypassing_session, tenant_a).list_tools(agent_id)
    by_name = {tool.name: is_enabled for tool, is_enabled in pairs}
    assert by_name["create_lead"] is False
    # Still a LEFT join: a tool the agent has no link to at all must keep
    # being reported, not vanish -- the failure mode of putting this predicate
    # in the WHERE clause instead of the ON clause.
    assert set(by_name) >= {"create_lead", "retrieve_knowledge"}


async def test_set_tool_enabled_never_flips_a_foreign_labelled_link(
    rls_bypassing_session, owner_connection, tenant_a, agent_with_a_foreign_labelled_link
):
    """The dashboard's write half. Whatever this call does, the one thing it
    may not do is flip a row labelled with another organization: with layer 1
    in place the row is not found, the upsert attempts an INSERT, and
    `agent_tools`' composite primary key refuses it. The assertion that
    matters is the one after the call -- the foreign row is untouched."""
    agent_id, tool_id = agent_with_a_foreign_labelled_link
    service = AgentService(rls_bypassing_session, tenant_a)
    try:
        with pytest.raises(Exception):  # noqa: B017 - see the docstring
            await service.set_tool_enabled(agent_id, tool_id, False)
    finally:
        # In a `finally`, not after the block: if this ever DOES flip the row
        # (i.e. layer 1 has regressed and nothing raised), the session is
        # left holding a row lock on `agent_tools`, and the fixture's own
        # teardown DELETE then blocks forever -- a failing assertion would
        # present as a hung suite instead of a red test.
        await rls_bypassing_session.rollback()

    row = await owner_connection.execute(
        text(
            "SELECT organization_id, is_enabled FROM agent_tools "
            "WHERE agent_id = :agent AND tool_id = :tool"
        ),
        {"agent": agent_id, "tool": tool_id},
    )
    organization_id, is_enabled = row.one()
    assert organization_id != tenant_a.organization_id
    assert is_enabled is True


async def test_record_tool_calls_refuses_another_tenants_message_id(
    rls_bypassing_session, tenant_a, tenant_b
):
    """`ChatService._assert_message_belongs_to_tenant` (whole-branch review,
    Minor 2). A Postgres FK constraint check runs with elevated privileges and
    does not consult RLS, so an INSERT into `message_tool_calls` or
    `message_citations` would happily attach a row to another tenant's
    `message_id` that a plain SELECT under RLS cannot even see. The guard was
    unpinned -- no-op'ing it to `return` left 52 tests green, because its own
    docstring is right that today's one caller can never make it fire.

    On an RLS-bypassing session the guard is the only thing left, and this is
    the only test in the suite that can tell it apart from a no-op.
    """
    async with tenant_session(tenant_b) as session:
        agent = await AgentService(session, tenant_b).create_agent(agent_input("Org B Bot"))
        conversations = ConversationService(session, tenant_b)
        conversation = await conversations.create(
            agent.id, CreateConversationInput(channel=ConversationChannel.API)
        )
        foreign_message_id = uuid7()
        await conversations.append_message(
            conversation.id,
            AppendMessageInput(
                id=foreign_message_id, role=MessageRole.ASSISTANT, content="org B's answer"
            ),
        )

    service = ChatService(rls_bypassing_session, tenant_a)
    with pytest.raises(NotFoundError):
        await service._assert_message_belongs_to_tenant(foreign_message_id)
