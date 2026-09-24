"""`app/tools/runtime.py` (Phase 7 Task 1): the one tool runtime `ChatService`
and, from Task 3 onward, the MCP server both call. `docs/PHASE-7.md` §5,
"One tool runtime, two callers" -- this is a behaviour-preserving move of
code that used to live in `app/chat/service.py` as
`ChatService._resolve_enabled_tool_names`/`ChatService._build_registry`/
`_LockedSessionTool`, plus two new, narrow behaviour changes pinned here:
`ToolContext.conversation_id` becomes optional, and `create_lead` refuses a
call with none.

The heavier, pre-existing coverage for the moved logic itself (shadowing,
tenancy, timeouts, the locked-session concurrency fix) already lives in
`tests/integration/test_chat_tools.py`, `test_agent_tools_layer_1.py` and
`test_agent_service.py`, unmodified by this move (Review Focus 5 --
`ChatService` keeps thin wrapper methods those tests call by the same
names). This module only pins the three things Task 1's brief asks for at
the new, direct entry points.
"""

import uuid

import pytest
from sqlalchemy import select

from app.agents.service import AgentService
from app.core.tenancy import tenant_session
from app.db.builtin_tools import DEFAULT_ENABLED_TOOL_NAMES
from app.db.models import Lead
from app.llm.types import ToolUseBlock
from app.tools.base import ToolContext
from app.tools.leads import CreateLeadTool
from app.tools.runtime import build_granted_registry, resolve_enabled_tool_names
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


def _ctx(tenant, **overrides) -> ToolContext:
    payload: dict[str, object] = {
        "organization_id": tenant.organization_id,
        "agent_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ToolContext(**payload)


async def test_build_granted_registry_registers_only_granted_names(tenant_a):
    """The registry itself is the authorization boundary (`build_granted_registry`'s
    own docstring, whole-branch review Critical 1): a name not passed in
    `tool_names` is never constructed at all, so a call naming it falls
    through to `ToolRegistry.execute`'s ordinary unknown-tool path rather
    than running.
    """
    async with tenant_session(tenant_a) as session:
        registry = build_granted_registry(session, ["retrieve_knowledge"])

        # Granted: shown in specs, and callable.
        assert [spec.name for spec in registry.specs_for(["retrieve_knowledge"])] == [
            "retrieve_knowledge"
        ]

        # Not granted: `create_lead` is a real builtin (`BUILTIN_TOOL_CLASSES`)
        # but was never in `tool_names`, so it was never registered.
        ctx = _ctx(tenant_a)
        result = await registry.execute(
            ToolUseBlock(id="call-1", name="create_lead", input={}), ctx
        )
        assert result.is_error is True
        assert "unknown tool" in result.content


async def test_resolve_enabled_tool_names_for_a_fresh_agent_returns_the_default_set(tenant_a):
    """`AgentService.create_agent` links every new agent to the global
    defaults (`app/db/builtin_tools.py`'s `DEFAULT_ENABLED_TOOL_NAMES`) with
    no further setup -- so a fresh agent's resolved names, read through the
    new direct entry point, must be exactly that set.
    """
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(agent_input("Fresh Bot"))
        names = await resolve_enabled_tool_names(session, tenant_a, agent.id)

    assert set(names) == set(DEFAULT_ENABLED_TOOL_NAMES)


async def test_create_lead_without_a_conversation_id_refuses_before_any_db_work(tenant_a):
    """Phase 7 Task 1's one real behaviour change (`docs/PHASE-7.md` §5): an
    MCP call has no conversation at all, so `ToolContext.conversation_id` is
    optional now, and `create_lead` -- the one tool MCP never exposes --
    refuses outright rather than raising deep inside `LeadService.create`'s
    FK-bypass guard. Checked before the rate-limit call and before any DB
    work, so a call like this writes nothing.
    """
    async with tenant_session(tenant_a) as session:
        ctx = _ctx(tenant_a, conversation_id=None)
        result = await CreateLeadTool(session).execute(
            CreateLeadTool.args_model(
                name="Jane Prospect", email="jane@example.com", interest="pricing"
            ),
            ctx,
        )

    assert result.is_error is True
    assert result.content == "create_lead needs a conversation and is not available here."

    async with tenant_session(tenant_a) as session:
        rows = (
            await session.execute(
                select(Lead).where(Lead.organization_id == tenant_a.organization_id)
            )
        ).all()
    assert rows == []
