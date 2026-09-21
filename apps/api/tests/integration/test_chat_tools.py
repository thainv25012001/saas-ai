"""Task 7: the agent loop wired into `ChatService.send` -- retrieval as a
tool the model chooses to call, tool-call events on the SSE-facing event
stream, and `message_tool_calls` persistence.

Every chat turn below scripts `FakeProvider` with `turns=`, not `script=`:
this is what the Phase 4 testing note in `docs/PHASE-4.md` §3 calls out --
a loop that runs "until the model stops asking for tools" cannot be tested
against a fake that can never ask for one. `enable_builtin_tool` (from
`tests/conftest.py`) gives a test agent an `agent_tools` row before any of
that matters, since `ChatService._resolve_enabled_tool_names` (this task)
reads that table, not a hard-coded list -- an agent with no such row is
offered no tools at all, tool-calling model or not.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Document, DocumentSourceType, Lead, MessageCitation, MessageToolCall
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.rag.retrieve import RetrievalService
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


async def test_an_agent_with_no_agent_tools_row_is_offered_no_tools(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
    assert names == []


async def test_global_builtin_tool_is_resolved_when_no_org_override_exists(
    tenant_a, owner_connection
):
    global_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, NULL, 'retrieve_knowledge', 'builtin', '{}', true)"
        ),
        {"id": global_id},
    )
    await owner_connection.commit()
    try:
        async with tenant_session(tenant_a) as session:
            agent = await _agent(session, tenant_a)
            await session.execute(
                text(
                    "INSERT INTO agent_tools "
                    "(agent_id, tool_id, organization_id, is_enabled, overrides) "
                    "VALUES (:agent_id, :tool_id, :org, true, '{}')"
                ),
                {"agent_id": agent.id, "tool_id": global_id, "org": tenant_a.organization_id},
            )
            await session.flush()
            names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
        assert names == ["retrieve_knowledge"]
    finally:
        await owner_connection.execute(text("DELETE FROM tools WHERE id = :id"), {"id": global_id})
        await owner_connection.commit()


async def test_disabled_agent_tools_link_excludes_the_tool(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, is_enabled=False)

    async with tenant_session(tenant_a) as session:
        names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent_id)
    assert names == []


async def test_non_builtin_tool_type_is_never_offered_to_the_model(tenant_a):
    """Phase 4 ships no HTTP/MCP adapter (`docs/ARCHITECTURE.md` §8 --
    MCP is explicitly Phase 6), so a `type='http'` row -- legal per Task 3's
    schema, and exactly what `test_tool_schema.py`'s own fixtures create --
    must never reach the model as an offered tool: nothing could ever answer
    the call."""
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
    assert names == []


async def test_org_scoped_tool_link_shadows_a_global_builtin_of_the_same_name(
    tenant_a, owner_connection
):
    """The shadowing decision `_resolve_enabled_tool_names` documents: an
    org-scoped `tools` row of the same name as a global builtin fully
    replaces it for this agent, including whether the agent may call it at
    all. Here the org-scoped link is *disabled* while a separate link to the
    (enabled) global builtin exists -- proving the disabled, more specific
    row wins rather than the tool falling back to the enabled global one.

    Fail-check performed by hand: with the shadowing loop's `if name in
    shadowed: continue` guard removed, both rows are folded into the same
    dict key in row order and the *last* row processed wins regardless of
    scope -- since `ORDER BY ... organization_id IS NULL` still places the
    org-scoped row first, the global (enabled) row would be processed
    second and overwrite it, resolving to `["retrieve_knowledge"]` instead
    of `[]`. Restored afterwards.
    """
    global_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, NULL, 'retrieve_knowledge', 'builtin', '{}', true)"
        ),
        {"id": global_id},
    )
    await owner_connection.commit()
    try:
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
                    "VALUES (:agent_id, :org_tool_id, :org, false, '{}'), "
                    "(:agent_id, :global_tool_id, :org, true, '{}')"
                ),
                {
                    "agent_id": agent.id,
                    "org_tool_id": org_tool_id,
                    "global_tool_id": global_id,
                    "org": tenant_a.organization_id,
                },
            )
            await session.flush()
            names = await ChatService(session, tenant_a)._resolve_enabled_tool_names(agent.id)
        assert names == []
    finally:
        await owner_connection.execute(text("DELETE FROM tools WHERE id = :id"), {"id": global_id})
        await owner_connection.commit()


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
    assert [t.name for t in provider.last_request.tools] == ["retrieve_knowledge"]


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


async def test_tool_context_agent_id_comes_from_the_conversation_not_the_request_parameter(
    tenant_a, owner_connection
):
    """`ToolContext.agent_id` must be server-derived from the conversation's
    own row -- `LeadService.create` (Task 6) deliberately skips re-checking
    `agent_id` against the conversation it is handed, relying on that
    invariant already holding by the time a `ToolContext` reaches it.
    `ChatService.send` itself never validates that the `agent_id` parameter
    a caller passes agrees with an existing `conversation_id`'s own agent,
    so this constructs exactly that mismatch -- two agents in the same org,
    a conversation that belongs to the first, a `send()` call naming the
    second -- and proves the resulting `create_lead` row is attributed to
    the conversation's own agent, not the mismatched request parameter.
    """
    async with tenant_session(tenant_a) as session:
        agent_one = await _agent(session, tenant_a, name="Agent One")
        agent_two = await _agent(session, tenant_a, name="Agent Two")
        agent_one_id, agent_two_id = agent_one.id, agent_two.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_one_id, tool_name="create_lead")
    await enable_builtin_tool(owner_connection, tenant_a, agent_two_id, tool_name="create_lead")

    # Turn 1: create the conversation under agent_one.
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=FakeProvider(turns=["hi"]))
        starts = [
            e async for e in service.send(agent_one_id, "hello") if isinstance(e, ChatMessageStart)
        ]
        conversation_id = starts[0].conversation_id

    # Turn 2: continue the SAME conversation, but naming agent_two.
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
        events = [
            event
            async for event in service.send(
                agent_two_id, "sign me up", conversation_id=conversation_id
            )
        ]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is False

    async with tenant_session(tenant_a) as session:
        lead = (
            await session.execute(select(Lead).where(Lead.conversation_id == conversation_id))
        ).scalar_one()
    assert lead.agent_id == agent_one_id
    assert lead.agent_id != agent_two_id
