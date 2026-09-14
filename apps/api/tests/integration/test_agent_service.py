import pytest

from app.agents.schemas import CreateAgentInput, UpdateAgentConfigInput, UpdateAgentInput
from app.agents.service import AgentService
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import tenant_session

pytestmark = pytest.mark.anyio


async def test_create_agent_assigns_a_slug(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.slug == "sales-bot"


async def test_create_agent_starts_in_draft(tenant_a):
    from app.db.models import AgentStatus

    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.status is AgentStatus.DRAFT


async def test_create_agent_creates_a_default_config(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        config = await service.get_config(agent.id)
    assert config.retrieval_top_k == 5
    assert config.max_agent_steps == 5
    assert config.enabled_tool_names == []


async def test_duplicate_slug_within_one_org_is_a_conflict(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        await service.create_agent(CreateAgentInput(name="Sales Bot"))
        with pytest.raises(ConflictError):
            await service.create_agent(CreateAgentInput(name="Sales Bot"))


async def test_the_same_slug_is_allowed_in_a_different_org(tenant_a, tenant_b):
    """Uniqueness is per-organization. Two businesses may both have a
    'sales-bot' — that is the entire point of scoping the constraint."""
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(CreateAgentInput(name="Sales Bot"))
    async with tenant_session(tenant_b) as session:
        agent = await AgentService(session, tenant_b).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.slug == "sales-bot"


async def test_list_returns_only_this_organizations_agents(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(CreateAgentInput(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        agents = await AgentService(session, tenant_b).list_agents()
    assert [a.name for a in agents] == []


async def test_get_agent_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(CreateAgentInput(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).get_agent(agent.id)


async def test_update_agent_changes_only_supplied_fields(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot", model="gpt-4o-mini"))
        updated = await service.update_agent(agent.id, UpdateAgentInput(temperature=0.2))
    assert updated.temperature == 0.2
    assert updated.model == "gpt-4o-mini"


async def test_update_config_persists(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        config = await service.update_config(
            agent.id, UpdateAgentConfigInput(tone="formal", retrieval_top_k=8)
        )
    assert config.tone == "formal"
    assert config.retrieval_top_k == 8


async def test_delete_removes_the_agent(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        await service.delete_agent(agent.id)
        with pytest.raises(NotFoundError):
            await service.get_agent(agent.id)


async def test_delete_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(CreateAgentInput(name="A Bot"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).delete_agent(agent.id)
