import pytest
from sqlalchemy import select, text

from app.agents.schemas import UpdateAgentConfigInput, UpdateAgentInput
from app.agents.service import AgentService
from app.chat.service import ChatService
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import tenant_session
from app.db.models import AgentToolLink, Tool
from app.llm.fake_provider import FakeProvider
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def test_create_agent_assigns_a_slug(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
    assert agent.slug == "sales-bot"


async def test_create_agent_starts_in_draft(tenant_a):
    from app.db.models import AgentStatus

    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
    assert agent.status is AgentStatus.DRAFT


async def test_create_agent_creates_a_default_config(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input(name="Sales Bot"))
        config = await service.get_config(agent.id)
    assert config.retrieval_top_k == 5
    assert config.max_agent_steps == 5
    # `enabled_tool_names` no longer exists on `agent_configs` -- Task 7b
    # removed it (see task-7b-report.md): it was read by nothing, and
    # `agent_tools` is the resolution source `ChatService` actually reads.
    # Which tools a new agent gets by default is covered by
    # test_create_agent_links_the_default_builtin_tool below.


async def test_duplicate_slug_within_one_org_is_a_conflict(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        await service.create_agent(agent_input(name="Sales Bot"))
        with pytest.raises(ConflictError):
            await service.create_agent(agent_input(name="Sales Bot"))


async def test_the_same_slug_is_allowed_in_a_different_org(tenant_a, tenant_b):
    """Uniqueness is per-organization. Two businesses may both have a
    'sales-bot' — that is the entire point of scoping the constraint."""
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
    async with tenant_session(tenant_b) as session:
        agent = await AgentService(session, tenant_b).create_agent(agent_input(name="Sales Bot"))
    assert agent.slug == "sales-bot"


async def test_list_returns_only_this_organizations_agents(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(agent_input(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        agents = await AgentService(session, tenant_b).list_agents()
    assert [a.name for a in agents] == []


async def test_get_agent_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).get_agent(agent.id)


async def test_update_agent_changes_only_supplied_fields(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input(name="Sales Bot", model="gpt-4o-mini"))
        updated = await service.update_agent(agent.id, UpdateAgentInput(temperature=0.2))
    assert updated.temperature == 0.2
    assert updated.model == "gpt-4o-mini"


async def test_update_agent_with_an_invalid_status_is_a_validation_error(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input(name="Sales Bot"))
        with pytest.raises(ValidationError):
            await service.update_agent(agent.id, UpdateAgentInput(status="bogus"))


async def test_update_agent_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).update_agent(
                agent.id, UpdateAgentInput(temperature=0.5)
            )


async def test_update_config_persists(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input(name="Sales Bot"))
        config = await service.update_config(
            agent.id, UpdateAgentConfigInput(tone="formal", retrieval_top_k=8)
        )
    assert config.tone == "formal"
    assert config.retrieval_top_k == 8


async def test_update_config_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).update_config(
                agent.id, UpdateAgentConfigInput(tone="formal")
            )


async def test_delete_removes_the_agent(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input(name="Sales Bot"))
        await service.delete_agent(agent.id)
        with pytest.raises(NotFoundError):
            await service.get_agent(agent.id)


async def test_delete_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).delete_agent(agent.id)


# ---------------------------------------------------------------------------
# Task 7b: make the tools reachable. `create_agent` links the default
# builtins in the same flush as the `AgentConfig` row, so a normally-created
# agent is never offered nothing -- see task-7b-report.md for the argument
# for on-by-default `retrieve_knowledge` / off-by-default `create_lead`.
# ---------------------------------------------------------------------------


async def test_create_agent_links_the_default_builtin_tool_in_the_same_flush(tenant_a):
    """`retrieve_knowledge` is a pure read with no risk, so it is linked
    (enabled) for every new agent by default. `create_lead` writes a real
    `leads` row every time it runs; today the only thing that can call it
    is an authenticated org member testing their own agent in the
    playground (no public, unauthenticated channel exists yet), so
    defaulting it on would let an ordinary test turn into a row in the
    exact table Task 8 presents to that same org as its customer pipeline.
    Phase 4 ships no dashboard/GraphQL surface yet to review or disable it
    per-agent, so it stays off by default (still reachable the same way
    `enable_builtin_tool` reaches it in tests -- a direct `agent_tools`
    insert).

    Read back within the SAME session `create_agent` used, before any
    later commit -- the row must already be visible off the one flush
    `create_agent` performed, not a later one.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
        rows = (
            await session.execute(
                select(Tool.name, Tool.organization_id)
                .select_from(AgentToolLink)
                .join(Tool, Tool.id == AgentToolLink.tool_id)
                .where(AgentToolLink.agent_id == agent.id)
            )
        ).all()
    names = {name for name, _org in rows}
    assert names == {"retrieve_knowledge"}
    # Linked to the GLOBAL builtin (seeded by migration 0009), not a
    # per-org copy: every new agent shares the one platform-wide row.
    assert all(org is None for _name, org in rows)


async def test_an_agent_created_through_create_agent_reaches_the_provider_with_tools_set(
    tenant_a,
):
    """The test that proves Phase 4 is no longer inert (task-7b brief,
    Test 2): an agent created the NORMAL way -- through
    `AgentService.create_agent`, with no test-only `enable_builtin_tool`
    fixture in sight -- driven through a real `ChatService.send` turn,
    must reach the provider with `tools` set. Asserted on the captured
    `CompletionRequest` itself, not on row counts: row counts would pass
    while the resolution path (`ChatService._resolve_enabled_tool_names`
    reading `config.enabled_tool_names`, which is read by nothing) stayed
    broken, exactly how this was missed the first time around.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
        agent_id = agent.id

    provider = FakeProvider(turns=["Hi there!"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "hello")]

    assert any(getattr(e, "text", None) for e in events)
    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    assert [t.name for t in provider.last_request.tools] == ["retrieve_knowledge"]


async def test_an_agent_with_its_tool_links_explicitly_cleared_is_offered_nothing(tenant_a):
    """The other half of the requirement-5 design decision (task-7b-report.md):
    `agent_tools` rows are the single source of truth with NO implicit
    "no links means every builtin" fallback. An agent whose links were
    deliberately all removed -- whether by an operator, or, before this
    migration existed, simply by predating it -- must stay offered
    nothing, not silently regain every builtin. Proven by removing the
    very links `create_agent` just made and resolving again.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input(name="Sales Bot"))
        await session.execute(
            text("DELETE FROM agent_tools WHERE agent_id = :id"), {"id": agent.id}
        )
        await session.flush()
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == []
