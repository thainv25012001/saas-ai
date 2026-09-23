import pytest
from sqlalchemy import select

from app.agents.service import AgentService
from app.core.tenancy import rolled_back_tenant_session, tenant_session
from app.db.models import Agent
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


class _Boom(Exception):
    """A stand-in for whatever a case's chat turn might raise -- the
    specific type does not matter, only that it propagates."""


async def test_a_row_inserted_inside_is_gone_afterwards(tenant_a):
    async with rolled_back_tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input("Rolled Back Agent"))
        agent_id = agent.id

    async with tenant_session(tenant_a) as session:
        agents = await AgentService(session, tenant_a).list_agents()
    assert agent_id not in [a.id for a in agents]


async def test_rls_is_applied_inside(tenant_a, tenant_b):
    """A query for another org's rows returns none -- `app.current_org_id` is
    set for this session exactly as `tenant_session` sets it, so RLS (and not
    merely the rollback at the end) is what a caller inside the block relies
    on for isolation."""
    async with tenant_session(tenant_b) as session:
        other_agent = await AgentService(session, tenant_b).create_agent(
            agent_input("Other Org's Agent")
        )

    async with rolled_back_tenant_session(tenant_a) as session:
        result = await session.execute(select(Agent).where(Agent.id == other_agent.id))
        assert result.scalar_one_or_none() is None


async def test_an_exception_inside_propagates_and_still_rolls_back(tenant_a):
    agent_id = None
    with pytest.raises(_Boom):
        async with rolled_back_tenant_session(tenant_a) as session:
            agent = await AgentService(session, tenant_a).create_agent(agent_input("Boom Agent"))
            agent_id = agent.id
            raise _Boom("the case's turn failed")

    assert agent_id is not None
    async with tenant_session(tenant_a) as session:
        agents = await AgentService(session, tenant_a).list_agents()
    assert agent_id not in [a.id for a in agents]
