"""`CreateLeadTool` (`app/tools/leads.py`): the only tool in this system
that writes data, and the only one an untrusted chat-widget visitor can
trigger (§7.2). Follows `tests/integration/test_retrieve_tool.py`'s shape
for the tenancy and savepoint tests -- the identical hazards apply here,
one layer further from whoever owns the session -- and adds the write-side
concerns a read-only tool never has: the schema-level contact requirement,
rate limiting, and the FK-bypasses-RLS check on `conversation_id`.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.agents.service import AgentService
from app.conversations.schemas import CreateConversationInput
from app.conversations.service import ConversationService
from app.core.tenancy import tenant_session
from app.db.models import ConversationChannel, LeadStatus
from app.leads.service import LeadService
from app.llm.types import ToolUseBlock
from app.tools.base import ToolContext
from app.tools.leads import _RATE_LIMIT, CreateLeadTool
from app.tools.registry import ToolRegistry
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _agent(session, tenant):
    return await AgentService(session, tenant).create_agent(agent_input("Sales Bot"))


async def _conversation(session, tenant, agent_id=None):
    if agent_id is None:
        agent = await _agent(session, tenant)
        agent_id = agent.id
    return await ConversationService(session, tenant).create(
        agent_id, CreateConversationInput(channel=ConversationChannel.PLAYGROUND)
    )


def _ctx(tenant, *, agent_id, conversation_id, **overrides) -> ToolContext:
    payload: dict[str, object] = {
        "organization_id": tenant.organization_id,
        "agent_id": agent_id,
        "conversation_id": conversation_id,
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ToolContext(**payload)


async def _leads_for_conversation(owner_connection, conversation_id) -> list:
    result = await owner_connection.execute(
        text("SELECT id FROM leads WHERE conversation_id = :cid"),
        {"cid": conversation_id},
    )
    return result.fetchall()


async def test_valid_call_creates_a_lead_scoped_to_the_org(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        tool = CreateLeadTool(session)
        result = await tool.execute(
            CreateLeadTool.args_model(
                name="Ada Lovelace", email="ADA@example.com", interest="pricing"
            ),
            _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id),
        )

    assert result.is_error is False
    assert result.data is not None
    lead_id = uuid.UUID(result.data["lead_id"])

    row = (
        await owner_connection.execute(
            text(
                "SELECT organization_id, agent_id, conversation_id, name, email, status "
                "FROM leads WHERE id = :id"
            ),
            {"id": lead_id},
        )
    ).one()
    assert row.organization_id == tenant_a.organization_id
    assert row.agent_id == conversation.agent_id
    assert row.conversation_id == conversation.id
    assert row.name == "Ada Lovelace"
    assert row.status == LeadStatus.NEW.value


async def test_email_is_lowercased_before_storage(tenant_a, owner_connection):
    """Documented normalisation, per the brief: case is not semantically
    meaningful in an email address for any mail provider a real visitor
    would use, so two submissions differing only by case must land as the
    same stored value."""
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        tool = CreateLeadTool(session)
        result = await tool.execute(
            CreateLeadTool.args_model(
                name="Grace Hopper", email="Grace.Hopper@EXAMPLE.com", interest="demo"
            ),
            _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id),
        )

    assert result.is_error is False
    assert result.data is not None
    lead_id = uuid.UUID(result.data["lead_id"])
    stored_email = (
        await owner_connection.execute(
            text("SELECT email FROM leads WHERE id = :id"), {"id": lead_id}
        )
    ).scalar_one()
    assert stored_email == "grace.hopper@example.com"


async def test_phone_cosmetic_formatting_is_stripped_before_storage(tenant_a, owner_connection):
    """Documented normalisation, per the brief: only cosmetic separators a
    human might type are stripped (spaces, hyphens, dots, parentheses) --
    deliberately not full E.164 parsing, which needs a region this tool has
    no reliable way to infer for an anonymous visitor."""
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        tool = CreateLeadTool(session)
        result = await tool.execute(
            CreateLeadTool.args_model(
                name="Alan Turing", phone="+1 (555) 123-4567", interest="enterprise plan"
            ),
            _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id),
        )

    assert result.is_error is False
    assert result.data is not None
    lead_id = uuid.UUID(result.data["lead_id"])
    stored_phone = (
        await owner_connection.execute(
            text("SELECT phone FROM leads WHERE id = :id"), {"id": lead_id}
        )
    ).scalar_one()
    assert stored_phone == "+15551234567"


async def test_neither_contact_method_is_an_error_naming_both(tenant_a, owner_connection):
    """§5.2's specific mechanism: with neither `email` nor `phone`, the
    args model itself must refuse the call -- routed through
    `ToolRegistry.execute` (not `tool.args_model(...)` directly) so this
    also proves the rejection happens before `CreateLeadTool.execute` ever
    runs, i.e. no database work is attempted at all."""
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

        registry = ToolRegistry()
        registry.register(CreateLeadTool(session))
        call = ToolUseBlock(
            id="call_1",
            name="create_lead",
            input={"name": "No Contact", "interest": "pricing"},
        )
        result = await registry.execute(
            call, _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id)
        )

    assert result.is_error is True
    assert "email" in result.content
    assert "phone" in result.content
    assert (await _leads_for_conversation(owner_connection, conversation.id)) == []


async def test_malformed_email_is_an_error_and_writes_nothing(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

        registry = ToolRegistry()
        registry.register(CreateLeadTool(session))
        call = ToolUseBlock(
            id="call_1",
            name="create_lead",
            input={"name": "Bad Email", "email": "not-an-email", "interest": "pricing"},
        )
        result = await registry.execute(
            call, _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id)
        )

    assert result.is_error is True
    assert "email" in result.content
    assert (await _leads_for_conversation(owner_connection, conversation.id)) == []


async def test_malformed_phone_is_an_error_and_writes_nothing(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

        registry = ToolRegistry()
        registry.register(CreateLeadTool(session))
        call = ToolUseBlock(
            id="call_1",
            name="create_lead",
            input={"name": "Bad Phone", "phone": "call me maybe", "interest": "pricing"},
        )
        result = await registry.execute(
            call, _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id)
        )

    assert result.is_error is True
    assert "phone" in result.content
    assert (await _leads_for_conversation(owner_connection, conversation.id)) == []


async def test_rate_limit_trips_to_is_error_and_writes_nothing(tenant_a, owner_connection):
    """§7.2/§7.3: exceeding the limit must be a result the model reads, not
    a 429 that ends the turn. `_RATE_LIMIT` calls succeed against one fresh
    conversation (a random uuid per test run, so this cannot collide with
    another test's Redis key); the next one over the limit must both fail
    and, unlike the earlier successes, write no row."""
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)
        ctx = _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id)

        for i in range(_RATE_LIMIT):
            tool = CreateLeadTool(session)
            result = await tool.execute(
                CreateLeadTool.args_model(
                    name=f"Visitor {i}", email=f"visitor{i}@example.com", interest="pricing"
                ),
                ctx,
            )
            assert result.is_error is False

        tool = CreateLeadTool(session)
        tripped = await tool.execute(
            CreateLeadTool.args_model(
                name="One Too Many", email="over-limit@example.com", interest="pricing"
            ),
            ctx,
        )

    assert tripped.is_error is True
    rows = await _leads_for_conversation(owner_connection, conversation.id)
    assert len(rows) == _RATE_LIMIT  # exactly the successful calls, not the tripped one


async def test_cross_tenant_conversation_id_is_refused_and_writes_nothing(
    tenant_a, tenant_b, owner_connection
):
    """The most important security fact in this codebase, restated for a
    write: an INSERT whose `conversation_id` FK points at another
    tenant's conversation succeeds regardless of RLS, because a Postgres FK
    integrity check runs with elevated privileges outside the referencing
    session's policy. Only `LeadService.create`'s explicit, tenant-scoped
    SELECT before the insert closes this -- this test fails if that check
    is removed, since RLS on `conversations` alone does nothing to stop the
    write (it would only stop org A reading org B's conversation row back,
    not stop the FK from being satisfied by it)."""
    async with tenant_session(tenant_b) as session:
        other_conversation = await _conversation(session, tenant_b)

    async with tenant_session(tenant_a) as session:
        registry = ToolRegistry()
        registry.register(CreateLeadTool(session))
        call = ToolUseBlock(
            id="call_1",
            name="create_lead",
            input={"name": "Cross Tenant", "email": "cross@example.com", "interest": "pricing"},
        )
        # `LeadService.create` raises `NotFoundError` (a plain Python
        # exception, not a `ToolResult`) -- routed through the registry, not
        # `tool.execute` directly, so this also confirms the tool leaves
        # that exception to propagate rather than swallowing it itself, the
        # same way `test_retrieve_tool.py`'s savepoint test does for the
        # SQL-level failure case.
        result = await registry.execute(
            call,
            _ctx(
                tenant_a,
                agent_id=other_conversation.agent_id,
                conversation_id=other_conversation.id,
            ),
        )

    assert result.is_error is True
    assert (await _leads_for_conversation(owner_connection, other_conversation.id)) == []


async def test_sql_level_failure_does_not_poison_the_callers_transaction(tenant_a, monkeypatch):
    """The savepoint ruling, restated for the one writing tool: a
    database-level failure inside `LeadService.create` must not abort the
    whole turn's transaction. Injects a genuine SQL-level failure -- a
    statement Postgres itself rejects -- on the *real* session, not a bare
    `RuntimeError`: a `RuntimeError` raised from a method that never
    touches the session would leave the transaction perfectly healthy and
    pass even with no savepoint at all, which is exactly how Phase 3 proved
    that class of test cannot catch this bug.
    """

    async def _broken_create(self, agent_id, conversation_id, data):
        await self.session.execute(text("SELECT * FROM this_table_does_not_exist_at_all"))
        raise AssertionError("unreachable: the statement above must raise first")

    monkeypatch.setattr(LeadService, "create", _broken_create)

    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        registry = ToolRegistry()
        registry.register(CreateLeadTool(session))
        ctx = _ctx(tenant_a, agent_id=conversation.agent_id, conversation_id=conversation.id)

        call = ToolUseBlock(
            id="call_1",
            name="create_lead",
            input={"name": "Broken", "email": "broken@example.com", "interest": "pricing"},
        )
        result = await registry.execute(call, ctx)

        assert result.is_error is True

        try:
            probe = await session.execute(text("SELECT 1"))
        except DBAPIError:
            pytest.fail("the caller's transaction was left poisoned by the tool's DB failure")
        assert probe.scalar_one() == 1

        monkeypatch.undo()
        second_call = ToolUseBlock(
            id="call_2",
            name="create_lead",
            input={"name": "Recovered", "email": "recovered@example.com", "interest": "pricing"},
        )
        second_result = await registry.execute(second_call, ctx)
        assert second_result.is_error is False
