"""Each isolation layer, proven on its own.

Every other cross-tenant test in this suite runs a service that filters on
`organization_id` inside a session that Postgres RLS has already bound to the
same organization. Both layers are active, so either one alone keeps those
tests green — delete the service filter and RLS covers it; neuter the RLS
predicate and the service filter covers it. `test_rls.py` only exercises the
`rls_probe` fixture table, and `test_migrations.py` asserts only that a policy
*named* `tenant_isolation` exists, never what its predicate says.

The two tests here each remove one layer from the picture so the other is the
only thing under test, on a real business table:

* Layer 2 (Postgres RLS) alone: raw SQL through `tenant_session`, no service.
* Layer 1 (application-layer filtering) alone: `AgentService` over an
  `app_owner` session, where RLS does not apply at all.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.service import AgentService
from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session

pytestmark = pytest.mark.anyio


@pytest.fixture
async def agents_in_two_orgs(
    owner_connection, tenant_a, tenant_b
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """One agent per organization, inserted as app_owner so neither RLS nor
    any service code is involved in creating them."""
    agent_a, agent_b = uuid7(), uuid7()
    for agent_id, tenant, name in (
        (agent_a, tenant_a, "Layer Probe A"),
        (agent_b, tenant_b, "Layer Probe B"),
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
    await owner_connection.commit()
    yield agent_a, agent_b
    await owner_connection.execute(
        text("DELETE FROM agents WHERE id IN (:a, :b)"), {"a": agent_a, "b": agent_b}
    )
    await owner_connection.commit()


async def test_rls_alone_hides_another_orgs_agents_from_raw_sql(
    agents_in_two_orgs, tenant_a: TenantContext, tenant_b: TenantContext
):
    """Layer 2 in isolation. No service, no ORM filter — a bare
    `SELECT ... FROM agents` with no WHERE clause at all, so the only thing
    that can keep B's row out of the result is the `tenant_isolation` policy
    on `agents`.

    Fail-check performed against the live database:
        ALTER POLICY tenant_isolation ON agents USING (true);
    made this test fail with both rows visible; the policy was then restored.
    """
    agent_a, agent_b = agents_in_two_orgs
    async with tenant_session(tenant_a) as session:
        rows = await session.execute(text("SELECT id, name FROM agents"))
        visible = {row.id: row.name for row in rows}

    # Positive control: an isolation bug that returned nothing at all would
    # satisfy the negative assertion on its own.
    assert agent_a in visible, "the owning tenant must still see its own agent"
    assert agent_b not in visible
    assert tenant_b.organization_id != tenant_a.organization_id


async def test_service_filter_alone_hides_another_orgs_agent_without_rls(
    agents_in_two_orgs, tenant_a: TenantContext
):
    """Layer 1 in isolation. The session is opened on the `app_owner` engine —
    the table owner, which bypasses row-level security entirely — so RLS
    contributes nothing here and the `organization_id` predicate inside
    `AgentService.get_agent` is the only thing standing between tenant A and
    tenant B's agent.

    Fail-check performed: removing
    `Agent.organization_id == self.tenant.organization_id`
    from `AgentService.get_agent` made this test fail (the agent was
    returned instead of raising NotFoundError); the filter was then restored.
    """
    agent_a, agent_b = agents_in_two_orgs
    engine = create_async_engine(_owner_url())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                # Positive control: RLS really is out of the picture here —
                # this same connection can see B's row when nothing filters it.
                unfiltered = await session.execute(
                    text("SELECT count(*) FROM agents WHERE id IN (:a, :b)"),
                    {"a": agent_a, "b": agent_b},
                )
                assert unfiltered.scalar_one() == 2

                service = AgentService(session, tenant_a)
                assert (await service.get_agent(agent_a)).id == agent_a
                with pytest.raises(NotFoundError):
                    await service.get_agent(agent_b)

                listed = {agent.id for agent in await service.list_agents()}
                assert agent_b not in listed
    finally:
        await engine.dispose()


def _owner_url() -> str:
    """The same engine the `owner_connection` fixture builds: app_owner owns
    the tables, and a table owner is exempt from its own RLS policies unless
    FORCE ROW LEVEL SECURITY is set (it is not)."""
    from app.core.config import get_settings

    return get_settings().migration_database_url
