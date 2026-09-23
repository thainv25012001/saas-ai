"""Task 7: the agent loop wired into `ChatService.send` -- retrieval as a
tool the model chooses to call, tool-call events on the SSE-facing event
stream, and `message_tool_calls` persistence.

Every chat turn below scripts `FakeProvider` with `turns=`, not `script=`:
this is what the Phase 4 testing note in `docs/PHASE-4.md` §3 calls out --
a loop that runs "until the model stops asking for tools" cannot be tested
against a fake that can never ask for one. `enable_builtin_tool` (from
`tests/conftest.py`) gives a test agent an ORG-SCOPED `agent_tools` row
before any of that matters, since `ChatService._resolve_enabled_tool_names`
(this task) reads that table, not a hard-coded list.

Since Task 7b, `_agent()`/`AgentService.create_agent` already links every
new agent to the GLOBAL `retrieve_knowledge` builtin by default (see
`app/db/builtin_tools.py`), so a test below that also calls
`enable_builtin_tool(..., tool_name="retrieve_knowledge")` is not creating
that tool's availability from nothing -- it is adding a more specific,
org-scoped link that *shadows* the default global one (same name, same
resolved outcome; see `_resolve_enabled_tool_names`'s own docstring on
shadowing). `create_lead` is NOT linked by default (task-7b-report.md), so
tests that need it still call `enable_builtin_tool(..., tool_name=
"create_lead")` for real.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import UpdateAgentConfigInput
from app.agents.service import AgentService
from app.chat.service import (
    ChatCitations,
    ChatError,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
    ChatToolCallEnd,
    ChatToolCallStart,
)
from app.conversations.service import ConversationService
from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Document, DocumentSourceType, Lead, MessageCitation, MessageToolCall
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.leads.service import LeadService
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.rag.retrieve import RetrievalService
from app.tools.retrieve import RetrieveKnowledgeTool
from tests.conftest import enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


async def _agent(session: AsyncSession, tenant: TenantContext, **overrides: object):  # type: ignore[no-untyped-def]
    name = overrides.pop("name", "Sales Bot")
    return await AgentService(session, tenant).create_agent(agent_input(name, **overrides))


async def _ready_document_with_chunks(
    session: AsyncSession,
    tenant: TenantContext,
    entries: list[tuple[str, list[float]]],
    title: str = "Doc",
) -> uuid.UUID:
    documents = DocumentService(session, tenant)
    document = await documents.create(
        CreateDocumentInput(title=title, source_type=DocumentSourceType.TEXT)
    )
    chunks = [
        ChunkInput(
            content=content,
            token_count=len(content.split()),
            embedding=embedding,
            embedding_model="hashing",
        )
        for content, embedding in entries
    ]
    await documents.replace_chunks(document.id, chunks)
    await documents.mark_ready(document.id)
    return document.id


# ---------------------------------------------------------------------------
# Tool resolution -- agent_tools joined to tools, and the shadowing decision
# ---------------------------------------------------------------------------


async def test_an_agent_created_normally_resolves_the_default_builtin_tool(tenant_a):
    """`AgentService.create_agent` (Task 7b) links the default builtin in
    the same flush as the config row, so an agent created the normal way
    -- no `enable_builtin_tool` fixture call in sight -- is never offered
    nothing. See `app/db/builtin_tools.py` and task-7b-report.md for why
    `retrieve_knowledge` specifically."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == ["get_product", "retrieve_knowledge", "search_products"]


async def test_an_agent_with_its_tool_links_explicitly_removed_is_offered_no_tools(tenant_a):
    """The no-implicit-fallback half of Task 7b's design (task-7b-report.md
    §"pre-existing agents"): `agent_tools` rows are the single source of
    truth, with nothing in `_resolve_enabled_tool_names` treating "no rows"
    as "every builtin". An agent whose links were deliberately all removed
    must stay offered nothing, not silently regain the default."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await session.execute(
            text("DELETE FROM agent_tools WHERE agent_id = :id"), {"id": agent.id}
        )
        await session.flush()
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == []


async def test_global_builtin_tool_is_resolved_when_no_org_override_exists(tenant_a):
    """Since Task 7b, `create_agent` itself is what links the agent to the
    global builtin (seeded once by migration 0009, not fabricated per test
    the way this test used to) -- so this scenario now falls straight out
    of creating an agent normally, with no org-scoped row anywhere to
    shadow it."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == ["get_product", "retrieve_knowledge", "search_products"]


async def test_disabled_agent_tools_link_excludes_the_tool(tenant_a, owner_connection):
    """Org-scoped, disabled `retrieve_knowledge` shadows and suppresses the
    global builtin of the same name (see `_resolve_enabled_tool_names`'s own
    shadowing docstring) -- but `get_product`/`search_products` are
    untouched by it and must still resolve, since Task 5 they are no
    longer the only other name in `DEFAULT_ENABLED_TOOL_NAMES`."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, is_enabled=False)

    async with tenant_session(tenant_a) as session:
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent_id)
    assert names == ["get_product", "search_products"]


async def test_non_builtin_tool_type_is_never_offered_to_the_model(tenant_a):
    """Phase 4 ships no HTTP/MCP adapter (`docs/ARCHITECTURE.md` §8 --
    MCP is explicitly Phase 6), so a `type='http'` row -- legal per Task 3's
    schema, and exactly what `test_tool_schema.py`'s own fixtures create --
    must never reach the model as an offered tool: nothing could ever answer
    the call. The three default builtins, not `[]`: since Task 7b (and,
    since Task 5, joined by `search_products`/`get_product`), `_agent()`
    already links every default builtin -- this proves the http-type row is
    excluded ON TOP of that, not that nothing at all is offered."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        http_tool_id = uuid7()
        await session.execute(
            text(
                "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
                "VALUES (:id, :org, 'private_crm_lookup', 'http', '{}', true)"
            ),
            {"id": http_tool_id, "org": tenant_a.organization_id},
        )
        await session.execute(
            text(
                "INSERT INTO agent_tools "
                "(agent_id, tool_id, organization_id, is_enabled, overrides) "
                "VALUES (:agent_id, :tool_id, :org, true, '{}')"
            ),
            {"agent_id": agent.id, "tool_id": http_tool_id, "org": tenant_a.organization_id},
        )
        await session.flush()
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == ["get_product", "retrieve_knowledge", "search_products"]


async def test_org_scoped_tool_link_shadows_a_global_builtin_of_the_same_name(tenant_a):
    """The shadowing decision `_resolve_enabled_tool_names` documents: an
    org-scoped `tools` row of the same name as a global builtin fully
    replaces it for this agent, including whether the agent may call it at
    all. Here the org-scoped link is *disabled* while a separate link to
    the (enabled) global builtin exists -- proving the disabled, more
    specific row wins rather than the tool falling back to the enabled
    global one.

    Since Task 7b, `_agent()` itself is what creates the enabled link to
    the global builtin (no manual insert needed any more -- migration 0009
    seeds the one global `retrieve_knowledge` row every test in this suite
    shares); this test only needs to add the org-scoped, disabled
    counterpart on top of it.

    Fail-check performed by hand (still valid against the new setup): with
    the shadowing loop's `if name in shadowed: continue` guard removed,
    both rows are folded into the same dict key in row order and the
    *last* row processed wins regardless of scope -- since `ORDER BY ...
    organization_id IS NULL` still places the org-scoped row first, the
    global (enabled) row would be processed second and overwrite it,
    resolving to `["get_product", "retrieve_knowledge", "search_products"]`
    instead of `["get_product", "search_products"]`. Restored afterwards.

    Since Task 5, `retrieve_knowledge` is not the only default builtin any
    more -- `get_product`/`search_products` are granted alongside it and
    are untouched by this test's shadowing, so they must still resolve.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        org_tool_id = uuid7()
        await session.execute(
            text(
                "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
                "VALUES (:id, :org, 'retrieve_knowledge', 'builtin', '{}', true)"
            ),
            {"id": org_tool_id, "org": tenant_a.organization_id},
        )
        await session.execute(
            text(
                "INSERT INTO agent_tools "
                "(agent_id, tool_id, organization_id, is_enabled, overrides) "
                "VALUES (:agent_id, :org_tool_id, :org, false, '{}')"
            ),
            {"agent_id": agent.id, "org_tool_id": org_tool_id, "org": tenant_a.organization_id},
        )
        await session.flush()
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == ["get_product", "search_products"]


# ---------------------------------------------------------------------------
# The agent loop, wired into a real chat turn
# ---------------------------------------------------------------------------


async def test_a_knowledge_question_triggers_the_tool_and_the_answer_cites(
    tenant_a, owner_connection
):
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session,
            tenant_a,
            [(f"{query} covers oil, filters, and brakes.", query_vector)],
            title="Maintenance Guide",
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="retrieve_knowledge", input={"query": query})],
            "Here's what the checklist covers.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [
            event async for event in service.send(agent_id, "What's on the maintenance checklist?")
        ]

    # tool_call_start precedes tool_call_end.
    start_index = next(i for i, e in enumerate(events) if isinstance(e, ChatToolCallStart))
    end_index = next(i for i, e in enumerate(events) if isinstance(e, ChatToolCallEnd))
    assert start_index < end_index

    tool_start = events[start_index]
    assert isinstance(tool_start, ChatToolCallStart)
    assert [c.name for c in tool_start.calls] == ["retrieve_knowledge"]
    assert tool_start.calls[0].arguments == {"query": query}

    tool_end = events[end_index]
    assert isinstance(tool_end, ChatToolCallEnd)
    assert tool_end.results[0].is_error is False
    assert tool_end.results[0].tool_call_id == "call_1"

    citation_events = [e for e in events if isinstance(e, ChatCitations)]
    assert len(citation_events) == 1
    assert len(citation_events[0].citations) >= 1
    # The citations event is emitted before message_end, per the new
    # ordering guarantee (see `ChatCitations`'s docstring).
    citations_index = events.index(citation_events[0])
    end_event_index = next(i for i, e in enumerate(events) if isinstance(e, ChatMessageEnd))
    assert citations_index < end_event_index

    text_out = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text_out == "Here's what the checklist covers."

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        citation_rows = (
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(citation_rows) == len(citation_events[0].citations)


async def test_a_greeting_triggers_no_tool_call_even_with_the_tool_available(
    tenant_a, owner_connection
):
    """The central Phase 4 claim (`docs/PHASE-4.md` §2): retrieval is no
    longer unconditional. Proven the strong way -- the tool spec really did
    reach the provider (`provider.last_request.tools`), so a passing test
    cannot be explained by the tool simply not being offered."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(turns=["Hi there! How can I help you today?"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "hi")]

    assert not any(isinstance(e, ChatToolCallStart) for e in events)
    assert not any(isinstance(e, ChatToolCallEnd) for e in events)
    assert not any(isinstance(e, ChatCitations) for e in events)
    text_out = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text_out == "Hi there! How can I help you today?"

    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    assert [t.name for t in provider.last_request.tools] == [
        "get_product",
        "retrieve_knowledge",
        "search_products",
    ]


async def test_a_failing_tool_surfaces_is_error_and_the_turn_still_completes(
    tenant_a, owner_connection, monkeypatch
):
    """Injects a genuine SQL-level failure (a statement Postgres itself
    rejects), not a bare `RuntimeError`, on the real session -- mirroring
    `test_retrieve_tool.py::test_sql_level_failure_does_not_poison_the_callers_transaction`,
    one layer up: that test proves the tool's own savepoint protects its
    immediate caller; this proves `ChatService.send`'s own later statements
    on the SAME session (`append_message`, `record_usage`, the
    `MessageToolCall`/`MessageCitation` writes) survive it too, and the
    turn still produces a normal `ChatMessageEnd`, not a `ChatError`.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    async def _broken_retrieve(self, query, **kwargs):  # type: ignore[no-untyped-def]
        await self.session.execute(text("SELECT * FROM this_table_does_not_exist_at_all"))
        raise AssertionError("unreachable: the statement above must raise first")

    monkeypatch.setattr(RetrievalService, "retrieve", _broken_retrieve)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="retrieve_knowledge", input={"query": "anything"})],
            "I couldn't check that, but here's what I can tell you.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "Tell me something")]

    assert not any(isinstance(e, ChatError) for e in events)
    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is True
    assert not any(isinstance(e, ChatCitations) for e in events)
    assert any(isinstance(e, ChatMessageEnd) for e in events)

    text_out = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text_out == "I couldn't check that, but here's what I can tell you."

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert len(history) == 2
    assert history[1].error is None
    assert history[1].content == text_out


async def test_message_tool_calls_persist_with_arguments_and_result(tenant_a, owner_connection):
    query = "warranty coverage duration"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} is explained here.", query_vector)], title="Warranty"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(
                    id="call_xyz",
                    name="retrieve_knowledge",
                    input={"query": query, "top_k": 3},
                )
            ],
            "It's one year.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "how long is the warranty?")]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.tool_call_id == "call_xyz"
    assert row.tool_name == "retrieve_knowledge"
    assert row.arguments == {"query": query, "top_k": 3}
    assert row.is_error is False
    assert row.error_message is None
    assert row.organization_id == tenant_a.organization_id
    assert row.result is not None
    assert isinstance(row.result["content"], str) and row.result["content"]
    # §3.6's declared column, populated now that each call is timed by
    # `_LockedSessionTool.execute` -- previously always left `None`.
    assert row.duration_ms is not None
    assert row.duration_ms >= 0
    assert len(row.result["citations"]) >= 1


async def test_a_failed_call_persists_a_message_tool_call_row_with_is_error_and_a_message(
    tenant_a, owner_connection, monkeypatch
):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    async def _boom(self, query, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("vector index outage")

    monkeypatch.setattr(RetrievalService, "retrieve", _boom)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="retrieve_knowledge", input={"query": "x"})],
            "Still here.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "hello")]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].is_error is True
    assert rows[0].error_message
    assert "failed unexpectedly" in rows[0].error_message


async def test_cross_tenant_tool_call_never_retrieves_another_orgs_chunk(
    tenant_a, tenant_b, owner_connection
):
    """Org B's chunk is seeded as the strongest possible lexical/vector match
    for org A's query (repeating the query text verbatim ten times) -- if
    tenancy ever leaked through the tool, this is exactly the chunk that
    would win and get cited."""
    query = "extended service agreement coverage terms"
    query_vector = await _embed(query)
    dominant_match = " ".join([query] * 10)

    async with tenant_session(tenant_b) as session:
        await _ready_document_with_chunks(
            session, tenant_b, [(dominant_match, query_vector)], title="Org B doc"
        )

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session,
            tenant_a,
            [(f"{query} is described briefly here.", query_vector)],
            title="Org A doc",
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(name="retrieve_knowledge", input={"query": query})],
            "Here's the coverage.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    citation_event = next(e for e in events if isinstance(e, ChatCitations))
    assert len(citation_event.citations) >= 1
    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))

    async with tenant_session(tenant_a) as session:
        org_a_document_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(Document.id).where(Document.organization_id == tenant_a.organization_id)
                )
            ).all()
        }
        citation_rows = (
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
        tool_call_rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )

    assert citation_rows  # confirms the query above actually found rows to check
    assert all(c.document_id in org_a_document_ids for c in citation_event.citations)
    assert all(row.document_id in org_a_document_ids for row in citation_rows)
    # The persisted tool-call record itself never mentions org B's content
    # either -- not just the citations table.
    assert len(tool_call_rows) == 1
    for citation in tool_call_rows[0].result["citations"]:
        assert uuid.UUID(citation["document_id"]) in org_a_document_ids


async def test_the_sse_tool_result_is_an_excerpt_never_the_full_payload(tenant_a, owner_connection):
    """`docs/PHASE-4.md`/the task brief: `tool_call_end` must never carry a
    tool's full result payload on the wire, the same reason `citations`
    carries an excerpt. `assemble_context`'s own framing text alone already
    exceeds the excerpt bound, so a single short chunk is enough to prove
    truncation -- and the DB row (`MessageToolCall.result`) is checked
    alongside it to prove the FULL text still reaches persistence; only the
    wire-facing copy is bounded.
    """
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} details.", query_vector)], title="Maintenance Guide"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="retrieve_knowledge", input={"query": query})],
            "ok",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    wire_result = tool_end.results[0].result
    assert len(wire_result) <= 240 + len("...")

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        row = (
            await session.execute(
                select(MessageToolCall).where(MessageToolCall.message_id == message_id)
            )
        ).scalar_one()
    # The persisted DB row keeps the FULL content -- only the wire copy is
    # bounded. If both were truncated identically, this would not
    # distinguish "excerpted for the wire" from "the tool's content was
    # simply already short" (which it is not -- assemble_context's framing
    # text alone is longer than the wire excerpt).
    assert len(row.result["content"]) > len(wire_result)
    assert row.result["content"].startswith(wire_result.removesuffix("..."))


async def test_a_conversation_id_naming_a_different_agent_than_the_request_is_rejected(
    tenant_a, owner_connection
):
    """Review round 1, Important finding 2: `ChatService.send` used to
    silently tolerate an `agent_id` parameter that disagreed with an
    existing `conversation_id`'s own `agent_id` -- `agent`/`provider_name`/
    `model_name`/`system_prompt`/`max_tokens`/`temperature` are all
    resolved from the REQUEST's `agent_id`, while `ToolContext.agent_id`
    (correctly) comes from `conversation.agent_id`, so a caller naming the
    wrong agent got a real mix: the wrong agent's prompt/provider/model
    answered, while any tool call that ran was attributed to (and could use
    the grants of) the conversation's OWN agent -- including a tool
    (`create_lead`) the request's named agent may never have been granted.

    Constructs exactly that mismatch -- two agents in the same org, a
    conversation belonging to the first, a `send()` call naming the second
    -- and proves the whole request is now refused before anything (a
    tool call, a persisted message) can happen, rather than silently
    picking one agent's identity for some fields and the other's for
    others.
    """
    async with tenant_session(tenant_a) as session:
        agent_one = await _agent(session, tenant_a, name="Agent One")
        agent_two = await _agent(session, tenant_a, name="Agent Two")
        agent_one_id, agent_two_id = agent_one.id, agent_two.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_two_id, tool_name="create_lead")

    # Turn 1: create the conversation under agent_one.
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=FakeProvider(turns=["hi"]))
        starts = [
            e async for e in service.send(agent_one_id, "hello") if isinstance(e, ChatMessageStart)
        ]
        conversation_id = starts[0].conversation_id

    # Turn 2: continue the SAME conversation, but naming agent_two -- must
    # be rejected before the scripted create_lead call (which would
    # otherwise succeed, since agent_two really does have it enabled) is
    # ever reached.
    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(
                    name="create_lead",
                    input={"name": "Jane", "email": "jane@example.com", "interest": "widgets"},
                )
            ],
            "Thanks, Jane!",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        with pytest.raises(NotFoundError):
            _ = [
                event
                async for event in service.send(
                    agent_two_id, "sign me up", conversation_id=conversation_id
                )
            ]

    async with tenant_session(tenant_a) as session:
        lead = (
            await session.execute(select(Lead).where(Lead.conversation_id == conversation_id))
        ).scalar_one_or_none()
    assert lead is None


async def test_two_real_tool_calls_in_one_step_do_not_corrupt_each_other(
    tenant_a, owner_connection
):
    """The concurrency hazard review round 1 found: `AgentRunner` gathers
    every call in a step with `asyncio.gather`, and an `AsyncSession` is
    not safe for concurrent use. Sharing one session across two tool
    instances meant a SUCCEEDING call could corrupt its sibling's
    `begin_nested()` savepoint, and `ToolRegistry.execute`'s generic
    exception handler turned that corruption into a fabricated
    `is_error=True` for a tool that never actually failed -- exactly
    backwards from §7.3's "one failing tool must not abort its siblings".

    Routed through `ChatService`, not a fake/in-memory registry: Task 4's
    own parallel-isolation tests (`tests/unit/test_agent_loop.py`) use a
    DB-free fake registry and could not have caught this -- the shared
    session only arrives at this seam. Both calls do REAL database work
    (one `retrieve_knowledge`, one `create_lead`), the shape that
    reproduced the bug.
    """
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} details.", query_vector)], title="Maintenance Guide"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="retrieve_knowledge")
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="create_lead")

    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(id="c1", name="retrieve_knowledge", input={"query": query}),
                FakeToolCall(
                    id="c2",
                    name="create_lead",
                    input={
                        "name": "Parallel Visitor",
                        "email": "parallel@example.com",
                        "interest": "widgets",
                    },
                ),
            ],
            "Done.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "help me")]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    results_by_id = {r.tool_call_id: r for r in tool_end.results}
    assert set(results_by_id) == {"c1", "c2"}
    assert results_by_id["c1"].is_error is False, results_by_id["c1"].result
    assert results_by_id["c2"].is_error is False, results_by_id["c2"].result

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        lead = (
            await session.execute(select(Lead).where(Lead.conversation_id == conversation_id))
        ).scalar_one_or_none()
    assert lead is not None
    assert lead.email == "parallel@example.com"


async def test_step_limit_reached_is_surfaced_as_an_in_band_error_not_a_silent_blank_message(
    tenant_a, owner_connection
):
    """Review round 1, Important finding 4: §5.1's pseudocode is `yield
    Error("step_limit_reached")`, but an earlier version of `ChatService.
    send` silently treated exhausting `max_agent_steps` as an ordinary
    success -- the client received a normal `message_end` carrying a
    possibly-empty assistant message, indistinguishable on the wire from
    the model genuinely finishing with nothing to say. `max_agent_steps=2`
    and a model that asks for a tool on every step forces the cap; this
    asserts the turn ends in `ChatError(code="step_limit_reached")`
    instead of `ChatMessageEnd`, while everything real about the turn
    (the two tool calls, usage) is still persisted.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
        await AgentService(session, tenant_a).update_config(
            agent_id, UpdateAgentConfigInput(max_agent_steps=2)
        )
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="c1", name="retrieve_knowledge", input={"query": "x"})],
            [FakeToolCall(id="c2", name="retrieve_knowledge", input={"query": "y"})],
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "keep looking")]

    assert not any(isinstance(e, ChatMessageEnd) for e in events)
    error_events = [e for e in events if isinstance(e, ChatError)]
    assert len(error_events) == 1
    assert error_events[0].code == "step_limit_reached"
    tool_ends = [e for e in events if isinstance(e, ChatToolCallEnd)]
    assert len(tool_ends) == 2

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert len(history) == 2
    assert history[1].finish_reason == "step_limit_reached"
    assert history[1].error is None  # not the AppError branch -- a real, if incomplete, turn

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        tool_call_rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(tool_call_rows) == 2


async def test_a_slow_sibling_holding_the_lock_does_not_fabricate_a_timeout_for_the_queued_call(
    tenant_a, owner_connection, monkeypatch
):
    """Review round 2, item 1: `_LockedSessionTool` serializes two calls in
    one step behind a shared lock, and `ToolRegistry.execute` wraps the
    WHOLE call (lock wait included) in `asyncio.timeout(tool.
    timeout_seconds)`. Without a separate, later-starting budget for the
    tool's own work, a slow sibling holding the lock can fabricate a
    timeout for a call that never got a chance to run -- reproduced here
    exactly as measured in review: `create_lead` (its own, unrestricted
    10s budget) genuinely takes ~1.0s of real work and holds the lock for
    it; `retrieve_knowledge`, queued behind it and given a tiny 0.3s OWN
    timeout, does negligible real work but must still complete rather than
    being reported as timed out, because its budget only starts once it
    actually acquires the lock -- two DIFFERENT tool classes, so each
    call's `timeout_seconds` can be set independently.
    """
    monkeypatch.setattr(RetrieveKnowledgeTool, "timeout_seconds", 0.3)

    real_create = LeadService.create

    async def _slow_create(self, agent_id, conversation_id, data):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1.0)
        return await real_create(self, agent_id, conversation_id, data)

    monkeypatch.setattr(LeadService, "create", _slow_create)

    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} details.", query_vector)], title="Maintenance Guide"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="retrieve_knowledge")
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="create_lead")

    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(
                    id="slow",
                    name="create_lead",
                    input={
                        "name": "Slow Visitor",
                        "email": "slow@example.com",
                        "interest": "widgets",
                    },
                ),
                FakeToolCall(id="fast", name="retrieve_knowledge", input={"query": query}),
            ],
            "done",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "help")]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    results_by_id = {r.tool_call_id: r for r in tool_end.results}
    assert set(results_by_id) == {"slow", "fast"}
    # Neither call is falsely reported as timed out -- specifically not
    # "fast", queued behind ~1.0s of "slow" holding the lock while its own
    # declared budget is only 0.3s.
    assert results_by_id["slow"].is_error is False, results_by_id["slow"].result
    assert results_by_id["fast"].is_error is False, results_by_id["fast"].result
    assert "timed out" not in results_by_id["fast"].result
    assert "took longer" not in results_by_id["fast"].result


async def test_an_ungranted_builtin_named_by_the_model_never_runs_and_writes_nothing(
    tenant_a, owner_connection
):
    """Whole-branch review, Critical 1. `_build_registry` used to register
    every builtin unconditionally and hand `_resolve_enabled_tool_names`'
    output to `AgentRunner` only to decide what the model is *shown*, so
    the `agent_tools` grant was advisory: a model that simply NAMED
    `create_lead` on an agent with no link to it -- a hallucination, or an
    instruction smuggled into an uploaded document and fed back as
    `retrieve_knowledge` tool-result content -- got it executed, writing a
    real `Lead` row with `is_error=False`.

    The agent here is created the normal way and never granted
    `create_lead` (Task 7b links only `retrieve_knowledge` by default), so
    the grant is the ONLY thing standing between the model's call and the
    write. Asserted on the `leads` table, not on the event stream alone:
    the point is that nothing ran, not merely that the stream said so.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent_id)
    assert names == ["get_product", "retrieve_knowledge", "search_products"], (
        "precondition: create_lead is NOT granted"
    )

    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(
                    id="smuggled",
                    name="create_lead",
                    input={
                        "name": "Mallory",
                        "email": "mallory@example.com",
                        "interest": "pwn",
                    },
                )
            ],
            "All set.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "sign me up")]

    # The model was never offered it either -- the pre-existing half of the
    # guarantee, asserted here so a regression in EITHER half fails.
    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    assert [t.name for t in provider.last_request.tools] == [
        "get_product",
        "retrieve_knowledge",
        "search_products",
    ]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert [r.tool_call_id for r in tool_end.results] == ["smuggled"]
    assert tool_end.results[0].is_error is True
    assert any(isinstance(e, ChatMessageEnd) for e in events)

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        leads = (
            (await session.execute(select(Lead).where(Lead.conversation_id == conversation_id)))
            .scalars()
            .all()
        )
        tool_call_rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(
                        MessageToolCall.tool_name == "create_lead",
                        MessageToolCall.tool_call_id == "smuggled",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert leads == [], "an ungranted tool must not write"
    assert len(tool_call_rows) == 1
    assert tool_call_rows[0].is_error is True


async def test_a_tool_whose_sql_outruns_its_budget_still_lets_the_turn_persist(
    tenant_a, owner_connection, monkeypatch
):
    """Whole-branch review, Critical 2, reproduced exactly as reported.

    `_LockedSessionTool` used to wrap the tool call in a bare
    `asyncio.timeout`. Firing it while an asyncpg statement was in flight on
    the turn's SHARED session cancelled that statement mid-flight and
    SQLAlchemy invalidated the connection -- and a savepoint cannot recover
    a *cancelled* statement the way it recovers a failed one. The next
    statement `ChatService.send` ran (`append_message`) then raised
    `PendingRollbackError`, which is a `SQLAlchemyError` and not an
    `AppError`, so it escaped `send()` uncaught and the entire turn rolled
    back -- the user's message, the answer already on screen, the tool rows,
    the citations and the usage row.

    `pg_sleep(5)` on the caller's own session under a 0.5s budget is what a
    genuinely slow hybrid retrieval looks like to this layer; at a default
    budget of 10s and an unbounded corpus it is an ordinary bad day. The
    assertions are on the DATABASE, not the event stream: the point is that
    the turn survived, not merely that something said `is_error`.
    """
    monkeypatch.setattr(RetrieveKnowledgeTool, "timeout_seconds", 0.5)

    async def _sleepy_retrieve(self, query, **kwargs):  # type: ignore[no-untyped-def]
        await self.session.execute(text("SELECT pg_sleep(5)"))
        raise AssertionError("unreachable: the statement above must be cancelled first")

    monkeypatch.setattr(RetrievalService, "retrieve", _sleepy_retrieve)

    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="slow_sql", name="retrieve_knowledge", input={"query": "anything"})],
            "I could not look that up in time.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "Tell me something")]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is True
    assert "took longer than" in tool_end.results[0].result
    assert any(isinstance(e, ChatMessageEnd) for e in events)
    assert not any(isinstance(e, ChatError) for e in events)

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
        tool_call_rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    # The user's own message AND the assistant's answer both survived.
    assert [row.role.value for row in history] == ["user", "assistant"]
    assert history[1].content == "I could not look that up in time."
    assert len(tool_call_rows) == 1
    assert tool_call_rows[0].is_error is True


async def test_the_session_is_still_usable_for_the_rest_of_the_turn_after_a_tool_timeout(
    tenant_a, owner_connection, monkeypatch
):
    """The general form of Critical 2's requirement: no tool failure of any
    kind may leave the caller's session unusable. A timed-out call is
    followed, in the SAME turn and on the SAME session, by a second tool
    call that does real database work -- `create_lead`, a write -- which can
    only succeed if the timeout left the session fully intact rather than
    merely "not crashing immediately".
    """
    monkeypatch.setattr(RetrieveKnowledgeTool, "timeout_seconds", 0.5)

    async def _sleepy_retrieve(self, query, **kwargs):  # type: ignore[no-untyped-def]
        await self.session.execute(text("SELECT pg_sleep(5)"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(RetrievalService, "retrieve", _sleepy_retrieve)

    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="retrieve_knowledge")
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="create_lead")

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="slow_sql", name="retrieve_knowledge", input={"query": "anything"})],
            [
                FakeToolCall(
                    id="after",
                    name="create_lead",
                    input={
                        "name": "Still Working",
                        "email": "still@example.com",
                        "interest": "widgets",
                    },
                )
            ],
            "Done.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "help")]

    ends = [e for e in events if isinstance(e, ChatToolCallEnd)]
    assert len(ends) == 2
    assert ends[0].results[0].is_error is True
    assert ends[1].results[0].is_error is False, ends[1].results[0].result

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        lead = (
            await session.execute(select(Lead).where(Lead.conversation_id == conversation_id))
        ).scalar_one_or_none()
    assert lead is not None
    assert lead.email == "still@example.com"


async def test_oversized_and_nul_bearing_tool_call_fields_do_not_destroy_the_turn(
    tenant_a, owner_connection
):
    """Whole-branch review, Important 1. All three shapes below were
    reproduced as an uncaught `DBAPIError` out of `send()` AFTER the answer
    had streamed, rolling the turn back entirely: a 150-character
    hallucinated tool name and a 150-character provider `tool_call_id`
    (`message_tool_calls.tool_name`/`.tool_call_id` are both `varchar(100)`),
    and a NUL byte in the arguments, which JSONB rejects outright.

    `ToolRegistry.execute` already degrades the hallucinated NAME to an
    error result exactly as §7.3 requires -- so this is specifically about
    the layer after it: persistence must not be able to kill a turn the
    model, the registry and the stream all handled correctly.
    """
    long_name = "x" * 150
    long_id = "i" * 150
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(id=long_id, name=long_name, input={"query": "a"}),
                FakeToolCall(
                    id="nul", name="retrieve_knowledge", input={"query": "a" + chr(0) + "b"}
                ),
            ],
            "Handled.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "go")]

    assert not any(isinstance(e, ChatError) for e in events)
    assert any(isinstance(e, ChatMessageEnd) for e in events)

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = (
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
        history = await ConversationService(session, tenant_a).history(
            next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
        )
    by_name = {row.tool_name: row for row in rows}
    assert len(rows) == 2
    # Truncated to the column width rather than rejected -- the row is the
    # audit record of what the model actually did, and 100 characters of an
    # absurd name identify it perfectly well.
    assert long_name[:100] in by_name
    assert by_name[long_name[:100]].tool_call_id == long_id[:100]
    assert by_name[long_name[:100]].is_error is True
    nul_row = by_name["retrieve_knowledge"]
    assert nul_row.arguments == {"query": "ab"}
    # And the turn itself survived, which is the whole point.
    assert [row.role.value for row in history] == ["user", "assistant"]
    assert history[1].content == "Handled."


async def test_finish_reason_records_the_providers_own_stop_reason(tenant_a, owner_connection):
    """Whole-branch review, carried item: `messages.finish_reason` -- a Phase
    2 column -- was `None` on every normal completion, because
    `AgentRunner._run_step` dropped the `MessageEndEvent.stop_reason` every
    provider already yields. Restored here, through the real persistence
    path, and asserted after a TOOL-using turn so the value recorded is the
    last step's ("end_turn"), not the first's ("tool_use").
    """
    query = "warranty coverage duration"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} is explained here.", query_vector)], title="Warranty"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="c1", name="retrieve_knowledge", input={"query": query})],
            "Two years.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].finish_reason == "end_turn"


async def test_a_step_limit_still_wins_over_the_providers_stop_reason(tenant_a, owner_connection):
    """The loop's own verdict is the more important fact: a turn cut off
    mid-thought must not record the last step's `tool_use` as if the model
    had chosen to stop."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
        await AgentService(session, tenant_a).update_config(
            agent_id, UpdateAgentConfigInput(max_agent_steps=1)
        )
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[[FakeToolCall(id="c1", name="retrieve_knowledge", input={"query": "anything"})]]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "hello")]

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].finish_reason == "step_limit_reached"


async def test_a_tools_statement_timeout_does_not_outlive_the_tool_call(tenant_a, owner_connection):
    """`_LockedSessionTool._clear_statement_timeout` (re-review, item 2).

    `_run_bounded` issues `SET LOCAL statement_timeout` per call so a slow
    query is cancelled by Postgres rather than by the event loop. `SET LOCAL`
    is transaction-scoped and Postgres undoes it when the savepoint it was
    issued inside ROLLS BACK -- but NOT when that savepoint is RELEASED,
    which is what a successful call does. Without the explicit reset in
    `_run_bounded`'s `finally`, a single successful `retrieve_knowledge`
    therefore leaves the tool's 10s budget clamped on the turn's transaction
    for every write `send()` makes afterwards: the assistant message, the
    tool-call rows, the citations, the usage row. Nothing in the suite would
    notice -- replacing that `finally` body with `pass` leaves 48 tests green
    -- because none of those writes is slow enough to hit 10s.

    Asserted on `current_setting('statement_timeout')` read from the SAME
    session, immediately after a turn that really did call the tool, because
    that is the value the caller's own later statements would run under.
    `'0'` is Postgres' spelling of "no limit", which is what this session
    began with and must end with.
    """
    query = "warranty coverage duration"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(f"{query} is explained here.", query_vector)], title="Warranty"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="c1", name="retrieve_knowledge", input={"query": query})],
            "Two years.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]
        # Same session, same still-open transaction the turn's own writes ran
        # in -- reading it on a fresh session would prove nothing, since
        # `SET LOCAL` never escapes the transaction that issued it.
        setting = (
            await session.execute(text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()

    # Precondition: the tool really ran, so a timeout really was set and
    # really had to be cleared.
    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is False, tool_end.results[0].result
    assert setting == "0", (
        "a tool's statement_timeout outlived its call and is now clamping "
        f"the rest of the turn's transaction at {setting}"
    )
