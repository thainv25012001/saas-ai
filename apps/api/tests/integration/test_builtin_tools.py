"""Task 7b: make the tools reachable.

Phase 4's own review found it shipped inert: a freshly migrated database had
zero `tools` rows and zero `agent_tools` rows, nothing but a test helper
(`tests/conftest.py::enable_builtin_tool`) could create either, and an agent
created the normal way reached the provider with `tools=None`. This module
covers the brief's numbered tests directly:

1. `test_seed_migration_creates_both_global_builtins` -- a freshly migrated
   database has both builtins, globally scoped.
2. The critical "no longer inert" assertion lives in
   `tests/integration/test_agent_service.py::
   test_an_agent_created_through_create_agent_reaches_the_provider_with_tools_set`
   (an agent created through `AgentService.create_agent`, driven through
   `ChatService`, reaches the provider with `tools` set, asserted on the
   captured `CompletionRequest`) -- not duplicated here, but exercised
   again below (`test_create_lead_is_reachable_once_explicitly_linked`)
   for the tool that is NOT on by default.
3. `test_seed_and_backfill_sql_are_safe_to_run_twice` -- the migration's
   own idempotent statements, run twice directly (see its docstring for why
   this is not an in-process `alembic downgrade`/`upgrade` -- nothing else
   in this codebase drives alembic from pytest, and doing so against the
   shared test database mid-suite is a real risk this module does not take;
   the true end-to-end round trip is verified once via the CLI and recorded
   in task-7b-report.md).
4. `enabled_tool_names` was removed outright (task-7b-report.md's
   decision), not translated -- `test_removed_field_is_gone_everywhere`
   pins that.
5. `test_a_pre_existing_agent_is_backfilled_onto_the_default_builtin` --
   an agent that predates the migration (simulated by inserting directly,
   bypassing `AgentService`, the same way a pre-Task-7b agent would exist)
   is backfilled by re-running the migration's own backfill statement.
6. `test_an_orgs_agent_cannot_resolve_another_orgs_tool_link` -- tenancy:
   one org's agent cannot resolve another org's tool link, and the global
   builtins stay visible to both.
"""

import pytest
from sqlalchemy import text

from app.agents.service import AgentService
from app.chat.service import ChatService
from app.core.ids import uuid7
from app.core.tenancy import tenant_session
from app.db.builtin_tools import (
    DEFAULT_ENABLED_TOOL_NAMES,
    default_agent_tools_backfill_sql,
    seed_tools_sql,
)
from app.llm.fake_provider import FakeProvider
from app.tools.leads import CreateLeadTool
from app.tools.retrieve import RetrieveKnowledgeTool
from tests.conftest import enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test 1: freshly migrated, both builtins present and global.
# ---------------------------------------------------------------------------


async def test_seed_migration_creates_both_global_builtins(owner_connection):
    rows = (
        await owner_connection.execute(
            text(
                "SELECT name, type, is_enabled FROM tools WHERE organization_id IS NULL "
                "AND name IN ('retrieve_knowledge', 'create_lead')"
            )
        )
    ).all()
    by_name = {row.name: row for row in rows}
    assert set(by_name) == {"retrieve_knowledge", "create_lead"}
    for row in by_name.values():
        assert row.type == "builtin"
        assert row.is_enabled is True


async def test_seeded_descriptions_match_the_tools_own_declared_descriptions(owner_connection):
    """Migration 0009 copies each description as literal text rather than
    importing the tool classes (see that migration's module docstring for
    why) -- this is the CI-enforced check that the copy has not drifted
    from what the tool itself declares. A drift here means the model is
    told one thing by its tool spec and the code does another."""
    rows = (
        await owner_connection.execute(
            text(
                "SELECT name, description FROM tools WHERE organization_id IS NULL "
                "AND name IN ('retrieve_knowledge', 'create_lead')"
            )
        )
    ).all()
    seeded = {row.name: row.description for row in rows}
    assert seeded["retrieve_knowledge"] == RetrieveKnowledgeTool.description
    assert seeded["create_lead"] == CreateLeadTool.description


# ---------------------------------------------------------------------------
# Test 2 (the tool NOT on by default): create_lead is reachable once an
# operator links it, the same way `enable_builtin_tool` does in every other
# Task 7 test -- the critical "reaches the provider with tools set"
# assertion for the default-on tool lives in test_agent_service.py.
# ---------------------------------------------------------------------------


async def test_create_lead_is_reachable_once_explicitly_linked(tenant_a, owner_connection):
    """`create_lead` defaults OFF (task-7b-report.md) but must still be
    reachable, not merely present in `tools` with no path to an agent ever
    offering it -- that would just be a narrower version of the exact
    inertness this task exists to fix."""
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input("Sales Bot"))
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="create_lead")

    provider = FakeProvider(turns=["hi"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        _ = [event async for event in service.send(agent_id, "hello")]

    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    names = {t.name for t in provider.last_request.tools}
    assert names == {"retrieve_knowledge", "create_lead"}


# ---------------------------------------------------------------------------
# Test 3: the migration's own idempotent statements, run twice directly.
# ---------------------------------------------------------------------------


async def test_seed_sql_is_safe_to_run_twice(owner_connection):
    """`seed_tools_sql()` is the exact statement migration 0009 runs to
    seed a global builtin -- exercised here with a throwaway name (not the
    real `retrieve_knowledge`/`create_lead`, which are already seeded and
    whose own idempotency is what makes THIS test possible to run
    alongside every other test in the suite) so this is a real assertion
    about the ON CONFLICT clause, not a tautology against a row nothing
    else could have raced to insert.
    """
    probe_id = uuid7()
    row = {
        "id": probe_id,
        "name": "_test_idempotent_probe_tool",
        "type": "builtin",
        "description": "probe",
    }
    try:
        await owner_connection.execute(text(seed_tools_sql()), row)
        # A second, DIFFERENT id for the SAME name: if ON CONFLICT ever
        # stopped matching (e.g. the partial index it targets were
        # dropped), this would insert a second row rather than being
        # absorbed, and the count assertion below would catch it.
        await owner_connection.execute(text(seed_tools_sql()), {**row, "id": uuid7()})
        await owner_connection.commit()

        count = (
            await owner_connection.execute(
                text("SELECT count(*) FROM tools WHERE organization_id IS NULL AND name = :name"),
                {"name": row["name"]},
            )
        ).scalar_one()
        assert count == 1
    finally:
        await owner_connection.execute(
            text("DELETE FROM tools WHERE name = :name AND organization_id IS NULL"),
            {"name": row["name"]},
        )
        await owner_connection.commit()


async def test_backfill_sql_is_safe_to_run_twice(tenant_a, owner_connection):
    """`default_agent_tools_backfill_sql()` is the exact statement
    migration 0009 runs to link a pre-existing agent onto the default
    builtin. Run here against an agent that already has the link
    (`create_agent` made it) -- a second run must not duplicate the row or
    error on `agent_tools`'s own primary key.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input("Sales Bot"))
        agent_id = agent.id

    for _ in range(2):
        await owner_connection.execute(
            text(default_agent_tools_backfill_sql()),
            {"names": list(DEFAULT_ENABLED_TOOL_NAMES)},
        )
        await owner_connection.commit()

    count = (
        await owner_connection.execute(
            text("SELECT count(*) FROM agent_tools WHERE agent_id = :id"), {"id": agent_id}
        )
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Test 4: enabled_tool_names is gone, not translated.
# ---------------------------------------------------------------------------


async def test_enabled_tool_names_column_no_longer_exists(owner_connection):
    """Pins the task-7b decision (task-7b-report.md): removed from the
    schema outright, not merely hidden from GraphQL. A writable field wired
    to nothing is worse than no field -- see that report for why removal
    was chosen over translating writes into `agent_tools` rows."""
    exists = (
        await owner_connection.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'agent_configs' AND column_name = 'enabled_tool_names'"
            )
        )
    ).first()
    assert exists is None


async def test_update_agent_config_input_has_no_enabled_tool_names_field():
    from app.agents.schemas import UpdateAgentConfigInput

    assert "enabled_tool_names" not in UpdateAgentConfigInput.model_fields


async def test_graphql_update_agent_config_input_has_no_enabled_tool_names_field():
    import app.graphql.types as gql

    assert not hasattr(gql.UpdateAgentConfigInput, "enabled_tool_names")
    assert not hasattr(gql.AgentConfig, "enabled_tool_names")


# ---------------------------------------------------------------------------
# Test 5: an agent predating the migration.
# ---------------------------------------------------------------------------


async def test_a_pre_existing_agent_is_backfilled_onto_the_default_builtin(
    tenant_a, owner_connection
):
    """Simulates an agent that predates migration 0009: inserted directly,
    bypassing `AgentService.create_agent` entirely, so it has zero
    `agent_tools` rows -- exactly the shape a real pre-Task-7b agent has.
    Re-running the migration's own backfill statement (rather than the
    whole migration -- see this module's docstring) must link it onto the
    default builtin, matching what a brand-new agent gets for free.
    """
    agent_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO agents "
            "(id, organization_id, name, slug, status, provider, model, "
            " temperature, max_tokens, public_key) "
            "VALUES (:id, :org, 'Legacy Bot', 'legacy-bot', 'draft', 'fake', 'fake-1', "
            " 0.3, 1024, :public_key)"
        ),
        {
            "id": agent_id,
            "org": tenant_a.organization_id,
            "public_key": f"pk_test_legacy_{agent_id.hex[:16]}",
        },
    )
    await owner_connection.commit()

    pre_count = (
        await owner_connection.execute(
            text("SELECT count(*) FROM agent_tools WHERE agent_id = :id"), {"id": agent_id}
        )
    ).scalar_one()
    assert pre_count == 0

    await owner_connection.execute(
        text(default_agent_tools_backfill_sql()),
        {"names": list(DEFAULT_ENABLED_TOOL_NAMES)},
    )
    await owner_connection.commit()

    async with tenant_session(tenant_a) as session:
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent_id)
    assert names == ["retrieve_knowledge"]


# ---------------------------------------------------------------------------
# Test 6: tenancy.
# ---------------------------------------------------------------------------


async def test_an_orgs_agent_cannot_resolve_another_orgs_tool_link(
    tenant_a, tenant_b, owner_connection
):
    async with tenant_session(tenant_a) as session:
        agent_a = await AgentService(session, tenant_a).create_agent(agent_input("A Bot"))
        agent_a_id = agent_a.id
    async with tenant_session(tenant_b) as session:
        agent_b = await AgentService(session, tenant_b).create_agent(agent_input("B Bot"))
        agent_b_id = agent_b.id
    await enable_builtin_tool(owner_connection, tenant_b, agent_b_id, tool_name="create_lead")

    # Org A's session, asked to resolve org B's agent: the explicit
    # organization_id filter in _resolve_enabled_tool_names (and RLS
    # underneath it) must both refuse to see org B's link.
    async with tenant_session(tenant_a) as session:
        names_cross_org = await ChatService(session, tenant_a)._resolve_enabled_tool_names(
            agent_b_id
        )
    assert names_cross_org == []

    # And the global builtin -- not org B's own create_lead link -- stays
    # visible to org A's OWN agent, while org B keeps both.
    async with tenant_session(tenant_a) as session:
        names_a = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent_a_id)
    async with tenant_session(tenant_b) as session:
        names_b = await ChatService(session, tenant_b)._resolve_enabled_tool_names(agent_b_id)
    assert names_a == ["retrieve_knowledge"]
    assert set(names_b) == {"retrieve_knowledge", "create_lead"}
