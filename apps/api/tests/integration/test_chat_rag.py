"""Task 7: grounded chat with citations, retargeted at retrieval-as-a-tool.

Every test in this module predates Task 7 and was originally written
against Phase 3's unconditional retrieval prefix: every chat turn against an
organization with a `ready` document retrieved, whether the question needed
it or not. `docs/PHASE-4.md` §2 is explicit that removing that prefix is
"the point of the phase" -- `retrieve_knowledge` is now a tool the model
chooses to call, so a test that wants a grounded turn has to script
`FakeProvider` with `turns=` (a `tool_use` block, then text) and give the
test agent an `agent_tools` row via `enable_builtin_tool`
(`tests/conftest.py`), not merely seed a `ready` document. See
`.superpowers/sdd/2026-09-20-phase-4-tools/task-7-report.md` for the
per-test before/after table this rewrite produced.

`HashingEmbedder` is used directly to embed seed content -- no network call
anywhere in this module, per the phase's constraints.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.tools.registry as tool_registry
from app.agents.service import AgentService
from app.api import chat as chat_api
from app.chat.service import (
    ChatCitations,
    ChatError,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
)
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import (
    DocumentSourceType,
    MembershipRole,
    MessageCitation,
    MessageToolCall,
)
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.llm.base import ModelCapabilities
from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.llm.types import (
    CompletionRequest,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseEvent,
    Usage,
    UsageEvent,
)
from app.main import create_app
from app.rag.retrieve import RetrievalService
from tests.conftest import enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

CHAT_URL = "/api/v1/chat/stream"

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


async def _agent(session, tenant, **overrides):  # type: ignore[no-untyped-def]
    name = overrides.pop("name", "Sales Bot")
    return await AgentService(session, tenant).create_agent(agent_input(name, **overrides))


async def _ready_document_with_chunks(
    session,  # type: ignore[no-untyped-def]
    tenant,  # type: ignore[no-untyped-def]
    entries: list[tuple[str, list[float]]],
    title: str = "Doc",
) -> uuid.UUID:
    """Create a document, write `entries` as chunks, and mark it ready.

    A `ready` document is necessary but no longer sufficient for a grounded
    turn -- the caller also needs `enable_builtin_tool` (giving the agent an
    `agent_tools` row) and a scripted `tool_use` call, since
    `retrieve_knowledge` is opt-in per turn now, not gated on this alone.
    """
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


class _ToolThenFailProvider:
    """One step calling a tool successfully, then a second step that raises
    partway through streaming text. `FakeProvider`'s `turns=` form
    explicitly rejects combining `fail_with` with `turns` (see its own
    `__init__`), so this hand-rolls the two-step shape directly, mirroring
    `tests/unit/test_agent_loop.py`'s `_FillerThenToolProvider`.
    """

    name = "fake"

    def __init__(self, tool_call_id: str, tool_name: str, tool_input: dict) -> None:  # type: ignore[type-arg]
        self._tool_call_id = tool_call_id
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._step = 0
        self.last_request: CompletionRequest | None = None

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest):  # type: ignore[no-untyped-def]
        raise NotImplementedError("only stream() is used")

    async def generate_structured(self, request, schema):  # type: ignore[no-untyped-def]
        raise NotImplementedError("only stream() is used")

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.last_request = request
        step, self._step = self._step, self._step + 1
        return self._stream(step)

    async def _stream(self, step: int) -> AsyncIterator[StreamEvent]:
        usage = Usage(input_tokens=1, output_tokens=1)
        yield MessageStartEvent(model=self.name)
        if step == 0:
            yield ToolUseEvent(
                block=ToolUseBlock(
                    id=self._tool_call_id, name=self._tool_name, input=self._tool_input
                )
            )
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason="tool_use", usage=usage, model=self.name)
        else:
            yield TextDeltaEvent(text="partial ")
            raise LLMUnavailableError("gone")


class _FillerThenRetrieveProvider:
    """Emits filler text AND a `tool_use` block in the SAME step --
    `FakeProvider`'s `turns=` form cannot script this (a scripted turn is
    prose OR calls, never both -- see `app/llm/fake_provider.py`), but it is
    exactly what a real provider routinely produces ("Let me check that for
    you," followed by the call). Used to prove Task 7's new `ChatCitations`
    ordering guarantee -- "before `message_end`", not "before the first
    `text_delta`" -- against a turn where text genuinely does precede the
    citations event, so passing is not an accident of the old ordering still
    happening to hold.
    """

    name = "fake"

    def __init__(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_input: dict,
        final_text: str,  # type: ignore[type-arg]
    ) -> None:
        self._tool_call_id = tool_call_id
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._final_text = final_text
        self._step = 0

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest):  # type: ignore[no-untyped-def]
        raise NotImplementedError("only stream() is used")

    async def generate_structured(self, request, schema):  # type: ignore[no-untyped-def]
        raise NotImplementedError("only stream() is used")

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        step, self._step = self._step, self._step + 1
        return self._stream(step)

    async def _stream(self, step: int) -> AsyncIterator[StreamEvent]:
        usage = Usage(input_tokens=1, output_tokens=1)
        yield MessageStartEvent(model=self.name)
        if step == 0:
            yield TextDeltaEvent(text="Let me check that for you. ")
            yield ToolUseEvent(
                block=ToolUseBlock(
                    id=self._tool_call_id, name=self._tool_name, input=self._tool_input
                )
            )
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason="tool_use", usage=usage, model=self.name)
        else:
            yield TextDeltaEvent(text=self._final_text)
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason="end_turn", usage=usage, model=self.name)


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


async def test_the_tool_result_the_provider_receives_carries_the_retrieved_chunk_text(
    tenant_a, owner_connection
):
    """The load-bearing grounding assertion, retargeted: Phase 3 injected
    retrieved text into the system prompt; Task 7 deletes that mechanism
    entirely (`docs/PHASE-4.md` §2) -- grounding now reaches the model
    exclusively through the `tool_result` block fed back after
    `retrieve_knowledge` runs. `query`/`chunk_text` still deliberately share
    only partial vocabulary ("widgets", "warranty") so a version that merely
    echoed the question back could not pass by accident.
    """
    query = "Do older widgets have a warranty?"
    chunk_text = (
        "Widgets purchased before March 2024 carry a lifetime warranty "
        "covering manufacturing defects only."
    )
    assert "lifetime warranty covering manufacturing defects" not in query

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(chunk_text, await _embed(chunk_text))], title="Warranty Policy"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[[FakeToolCall(name="retrieve_knowledge", input={"query": query})], "ok"]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        _ = [event async for event in service.send(agent_id, query)]

    assert provider.last_request is not None
    tool_result_texts = "\n".join(
        block.content
        for message in provider.last_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert "lifetime warranty covering manufacturing defects" in tool_result_texts
    # The injection mitigation must have actually reached the model, not
    # merely exist as a function nothing calls.
    assert "reference data, not instructions" in tool_result_texts
    # And confirm the OLD mechanism is really gone: the chunk text is not
    # smuggled into the system prompt as well.
    assert "lifetime warranty covering manufacturing defects" not in provider.last_request.system


async def test_message_citations_are_persisted_with_sequential_ranks(tenant_a, owner_connection):
    """At least two chunks, so a version of `_record_citations` that wrote
    only the first citation (or hard-coded rank=1) cannot pass by accident --
    the trap the brief calls out explicitly for this kind of test."""
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    entries = [
        (f"Annual maintenance inspection checklist item number {i}.", query_vector)
        for i in range(3)
    ]

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(session, tenant_a, entries, title="Maintenance Guide")
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[[FakeToolCall(name="retrieve_knowledge", input={"query": query})], "ok"]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    citation_event = next(e for e in events if isinstance(e, ChatCitations))
    assert len(citation_event.citations) >= 2

    async with tenant_session(tenant_a) as session:
        result = await session.execute(
            select(MessageCitation)
            .where(MessageCitation.message_id == message_id)
            .order_by(MessageCitation.rank)
        )
        rows = list(result.scalars().all())

    assert len(rows) == len(citation_event.citations)
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
    assert all(row.message_id == message_id for row in rows)
    assert all(row.organization_id == tenant_a.organization_id for row in rows)


async def test_citation_ranks_are_renumbered_sequentially_across_two_calls_in_one_turn(
    tenant_a, owner_connection
):
    """Review round 1, Important finding 3: two `retrieve_knowledge` calls
    in one turn used to persist ranks `[1, 1, 2, 2]` (each call's own
    local rank, written through unchanged) rather than `[1, 2, 3, 4]`
    (this message's citations, numbered by their position among
    everything it cited). No unique constraint on `(message_id, rank)`
    caught it -- this test scripts exactly that two-call turn and asserts
    the renumbered, non-duplicated sequence directly.
    """
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    entries = [
        (f"Annual maintenance inspection checklist item number {i}.", query_vector)
        for i in range(2)
    ]

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(session, tenant_a, entries, title="Maintenance Guide")
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(name="retrieve_knowledge", input={"query": query})],
            [FakeToolCall(name="retrieve_knowledge", input={"query": query})],
            "ok",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    citation_events = [e for e in events if isinstance(e, ChatCitations)]
    assert len(citation_events) == 2
    total_citations = sum(len(e.citations) for e in citation_events)
    assert total_citations == 4  # 2 calls x 2 chunks each
    # Each individual event still carries its OWN call's local rank
    # (1, 2) -- unaffected by the turn-wide renumbering that only applies
    # to what gets persisted.
    for event in citation_events:
        assert [c.rank for c in event.citations] == [1, 2]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = list(
            (
                await session.execute(
                    select(MessageCitation)
                    .where(MessageCitation.message_id == message_id)
                    .order_by(MessageCitation.rank)
                )
            ).scalars()
        )

    assert len(rows) == 4
    assert [row.rank for row in rows] == [1, 2, 3, 4]


async def test_a_greeting_never_calls_retrieval_even_when_the_tool_is_available(
    tenant_a, owner_connection, monkeypatch
):
    """Phase 3 pinned this for the old unconditional prefix: an organization
    with no corpus never called retrieval at all, gated on document
    readiness. Task 7 changes *why* that can be true -- there is no more
    readiness gate to check, only whether the model chose to call the tool
    -- so this seeds a `ready` document AND links `retrieve_knowledge` to
    the agent (`enable_builtin_tool`), and still proves
    `RetrievalService.retrieve` is never invoked for a plain greeting.
    Raising from the patched method (rather than counting calls) is what
    discriminates a real "never called" from "called and its result
    discarded", exactly as the original version of this test reasoned --
    now aimed at the model's own decision rather than a gate that no longer
    exists.
    """
    called = False

    async def _fail_if_called(self, query, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("retrieval must not run when the model does not call the tool")

    monkeypatch.setattr(RetrievalService, "retrieve", _fail_if_called)

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [("Some ready content.", await _embed("Some ready content."))]
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(turns=["hi"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "Hello")]

    assert called is False
    assert not any(isinstance(e, ChatCitations) for e in events)
    assert any(isinstance(e, ChatMessageEnd) for e in events)
    deltas = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert deltas == "hi"
    # The tool really was offered to the model -- a passing test above
    # cannot be explained by the tool simply being unavailable to ask for.
    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    # Alphabetical -- `_resolve_enabled_tool_names` orders by `Tool.name`.
    assert [t.name for t in provider.last_request.tools] == [
        "get_product",
        "retrieve_knowledge",
        "search_products",
    ]


async def test_a_failing_retrieval_tool_call_degrades_to_an_ungrounded_answer(
    tenant_a, owner_connection, monkeypatch
):
    """A retrieval outage must cost the user grounding, not their answer --
    the same property Phase 3 proved for the unconditional prefix, now
    proven for the tool path. The failure is caught (and logged) one layer
    down from before Task 7: `ToolRegistry.execute`'s generic exception
    handler (`app/tools/registry.py`), not a `ChatService`-owned retrieval
    step, which no longer exists.
    """

    async def _boom(self, query, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("vector index outage")

    monkeypatch.setattr(RetrievalService, "retrieve", _boom)

    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        tool_registry.logger,
        "exception",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [("Some ready content.", await _embed("Some ready content."))]
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[
            [FakeToolCall(name="retrieve_knowledge", input={"query": "anything"})],
            "Still here.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "Hello")]

    assert not any(isinstance(e, ChatCitations) for e in events)
    text = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text == "Still here."
    assert any(isinstance(e, ChatMessageEnd) for e in events)

    # `any(...)`, not a count: this pins that the failure was logged,
    # without coupling to exactly how many `logger.exception` calls this
    # path makes.
    assert any(entry["event"] == "tool_call_raised" for entry in logged)


async def test_message_citations_are_persisted_even_when_the_stream_fails_mid_turn(
    tenant_a, owner_connection
):
    """The assistant message on this branch is still persisted (with
    `error=` set) and was still generated using a tool result that included
    these chunks -- so it must still get citation rows, exactly like the
    success-path test above. Without this, a failed-but-grounded turn is
    indistinguishable from one that was never grounded at all.
    """
    chunk_text = "Our refund policy allows a full refund within thirty days of purchase."
    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(chunk_text, await _embed(chunk_text))]
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = _ToolThenFailProvider(
        tool_call_id="call_1",
        tool_name="retrieve_knowledge",
        tool_input={"query": "refund policy"},
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "What is the refund policy?")]

    assert any(isinstance(e, ChatError) for e in events)
    citation_event = next(e for e in events if isinstance(e, ChatCitations))
    assert citation_event.citations  # grounding was attempted before the stream failed
    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))

    async with tenant_session(tenant_a) as session:
        rows = list(
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            ).scalars()
        )
    assert len(rows) == len(citation_event.citations)

    # The tool call that produced them is recorded too, despite the later
    # provider failure -- Task 7's symmetry with `_record_citations`.
    async with tenant_session(tenant_a) as session:
        tool_rows = list(
            (
                await session.execute(
                    select(MessageToolCall).where(MessageToolCall.message_id == message_id)
                )
            ).scalars()
        )
    assert len(tool_rows) == 1
    assert tool_rows[0].is_error is False


async def test_a_citation_outlives_both_a_re_ingest_and_a_document_delete(
    tenant_a, owner_connection
):
    """`docs/PHASE-3.md` §5 says this table exists so a later phase can ask
    "did it answer from the sources?" of a turn that has already happened.
    A citation that vanishes the moment its chunk does cannot answer it.

    Both destructive paths are exercised, because they are the same DELETE
    seen from two directions:

    - re-ingest: `DocumentService.replace_chunks` deletes every chunk of the
      document before inserting the new ones, so with `ON DELETE CASCADE` on
      `chunk_id` a re-index wiped the history of every answer that document
      ever grounded, while the assistant messages themselves survived;
    - `deleteDocument`: cascades to `document_chunks`, and took the
      citations with it for the same reason. That one is reachable from the
      dashboard today.

    The row must stay, with `chunk_id`/`document_id` nulled out and the
    denormalised `document_title`/`excerpt` still saying what was cited.
    """
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    entries = [("Annual maintenance inspection checklist, revision four.", query_vector)]

    async with tenant_session(tenant_a) as session:
        document_id = await _ready_document_with_chunks(
            session, tenant_a, entries, title="Maintenance Guide"
        )
        agent = await _agent(session, tenant_a)
        agent_id = agent.id
    await enable_builtin_tool(owner_connection, tenant_a, agent_id)

    provider = FakeProvider(
        turns=[[FakeToolCall(name="retrieve_knowledge", input={"query": query})], "ok"]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, query)]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))

    async def _citations() -> list[MessageCitation]:
        async with tenant_session(tenant_a) as session:
            result = await session.execute(
                select(MessageCitation).where(MessageCitation.message_id == message_id)
            )
            return list(result.scalars().all())

    before = await _citations()
    assert len(before) >= 1
    assert all(row.document_title == "Maintenance Guide" for row in before)
    assert all("revision four" in row.excerpt for row in before)

    # Re-ingest: same document, fresh chunks, old chunk rows deleted.
    async with tenant_session(tenant_a) as session:
        await DocumentService(tenant=tenant_a, session=session).replace_chunks(
            document_id,
            [
                ChunkInput(
                    content="Annual maintenance inspection checklist, revision five.",
                    token_count=6,
                    embedding=query_vector,
                    embedding_model="hashing",
                )
            ],
        )

    after_reingest = await _citations()
    assert len(after_reingest) == len(before)
    assert all(row.chunk_id is None for row in after_reingest)
    assert all(row.document_id == document_id for row in after_reingest)
    assert all(row.document_title == "Maintenance Guide" for row in after_reingest)
    assert all("revision four" in row.excerpt for row in after_reingest)

    async with tenant_session(tenant_a) as session:
        await DocumentService(session, tenant_a).delete(document_id)

    after_delete = await _citations()
    assert len(after_delete) == len(before)
    assert all(row.document_id is None for row in after_delete)
    # Still legible with nothing left to join to -- the whole point of
    # denormalising these two columns onto the citation row.
    assert all(row.document_title == "Maintenance Guide" for row in after_delete)
    assert all("revision four" in row.excerpt for row in after_delete)

    # The message it cites is untouched by any of this.
    from sqlalchemy import text as sa_text

    async with tenant_session(tenant_a) as session:
        surviving = await session.execute(
            sa_text("SELECT COUNT(*) FROM messages WHERE id = :id"), {"id": message_id}
        )
    assert surviving.scalar_one() == 1


# ---------------------------------------------------------------------------
# SSE endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def api_client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _parse_events(body: str) -> list[dict[str, object]]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            import json

            events.append(json.loads(line[len("data: ") :]))
    return events


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Chat Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


async def _make_agent(org_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    name = str(overrides.pop("name", "Sales Bot"))
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name, **overrides))
    return agent.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_sse_stream_emits_citations_before_message_end_even_after_text_already_streamed(
    app, api_client, clean_users, owner_connection
):
    """Phase 3 pinned `citations` strictly between `message_start` and the
    first `text_delta`. Task 7 makes that impossible in general -- the model
    may talk first, then call `retrieve_knowledge`, then answer -- so this
    is the SAME test, updated rather than deleted, and retargeted at the
    guarantee that still holds: citations arrive before `message_end`
    (see `ChatCitations`'s docstring in `app/chat/service.py`).
    `_FillerThenRetrieveProvider` deliberately emits text BEFORE the tool
    call, so this cannot pass merely because citations happen to still come
    first by construction -- a `text_delta` genuinely precedes `citations`
    here, which the old assertion (`message_start < citations < text_delta`)
    would now correctly reject.

    Mutation-verified: temporarily changed the assertion back to
    `types.index("citations") < types.index("text_delta")` -- failed, as it
    must, since the filler text_delta is emitted first. Also temporarily
    moved `ChatService.send`'s `yield ChatCitations(...)` to after the
    event loop (so it would race `ChatMessageEnd`) -- this test failed with
    citations landing after message_end, confirming the `< message_end`
    half is still live. Both reverted; see the task report for exact
    commands.
    """
    token = await _register(api_client, "rag-citations-order@example.com", "Ada Motors Order")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    await enable_builtin_tool(owner_connection, tenant, agent_id)

    chunk_text = "Our standard warranty covers parts and labor for one full year."
    async with tenant_session(tenant) as session:
        await _ready_document_with_chunks(
            session, tenant, [(chunk_text, await _embed(chunk_text))], title="Warranty Policy"
        )

    provider = _FillerThenRetrieveProvider(
        tool_call_id="call_1",
        tool_name="retrieve_knowledge",
        tool_input={"query": "standard warranty coverage"},
        final_text="Here you go.",
    )
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL,
        json={
            "agent_id": str(agent_id),
            "message": "How long does the standard warranty cover parts and labor?",
        },
        headers=_auth(token),
    )

    events = _parse_events(response.text)
    types = [e["type"] for e in events]
    assert "citations" in types
    assert types.index("text_delta") < types.index("citations") < types.index("message_end")

    citation = events[types.index("citations")]["citations"][0]
    assert set(citation.keys()) == {
        "chunk_id",
        "document_id",
        "product_id",
        "document_title",
        "rank",
        "score",
        "excerpt",
        "page",
    }
    assert "warranty" in citation["excerpt"].lower()
    assert citation["page"] is None
    # A chunk citation, not a product one -- Task 5 widens this payload to
    # carry both kinds (`app/rag/retrieve.py::CitationPayload`), but this
    # turn never called a product tool.
    assert citation["product_id"] is None


async def test_sse_stream_with_no_tool_call_has_no_citations_or_tool_call_events(
    app, api_client, clean_users, owner_connection
):
    """The endpoint-level counterpart to the greeting test above -- proves
    the full SSE wire sequence stays exactly
    `[message_start, text_delta, message_end]` for a turn where the model,
    despite `retrieve_knowledge` being available (`enable_builtin_tool`),
    chooses not to call it. Phase 3's version of this test proved the same
    sequence for an org with *no documents at all*; Task 7 makes that case
    trivially true (there is nothing to retrieve from, but more importantly
    nothing is even offered without an `agent_tools` row), so this seeds the
    tool explicitly to prove the stronger claim -- the model's own choice,
    not the tool's absence, is what keeps the stream this short.

    Review round 1, minor finding: an earlier version of this test asserted
    only the event sequence, so mutating `ChatService.send` to always
    resolve `tool_names=[]` (silently never offering ANY tool to ANY
    turn) would still pass it -- indistinguishable from the model
    choosing not to call an available tool. The `provider.last_request.
    tools` assertion below is what tells those two apart.
    """
    token = await _register(api_client, "rag-no-call@example.com", "Ada Motors No Call")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    await enable_builtin_tool(owner_connection, tenant, agent_id)
    provider = FakeProvider(turns=["hi"])
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    events = _parse_events(response.text)
    assert [e["type"] for e in events] == ["message_start", "text_delta", "message_end"]
    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    # Alphabetical -- `_resolve_enabled_tool_names` orders by `Tool.name`.
    assert [t.name for t in provider.last_request.tools] == [
        "get_product",
        "retrieve_knowledge",
        "search_products",
    ]
