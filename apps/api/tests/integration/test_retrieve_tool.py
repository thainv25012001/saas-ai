"""`RetrieveKnowledgeTool` (`app/tools/retrieve.py`): Phase 4 Task 5's
central change -- retrieval as a tool the model chooses to call, rather
than a preprocessing step run before every turn.

Follows `tests/integration/test_retrieve.py`'s seeding style: chunks are
written directly through `DocumentService.replace_chunks` with an explicit
(content, embedding) pair, not through the real ingestion pipeline, since
what matters here is the tool's own behaviour -- clamping, the empty-result
message, tenancy, and the transaction-safety of its database work -- not
extraction or chunking. `HashingEmbedder` is used directly and explicitly
(never the settings-resolved default) so every test stays a real embedding
with no network call, per the brief.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.tenancy import tenant_session
from app.db.models import DocumentSourceType
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.llm.types import ToolUseBlock
from app.rag.retrieve import RetrievalService
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry
from app.tools.retrieve import _MAX_TOP_K, _NO_RESULTS_MESSAGE, RetrieveKnowledgeTool

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


def _negate(vector: list[float]) -> list[float]:
    return [-v for v in vector]


async def _document(session, tenant, title: str = "Doc"):
    """Mirrors `test_retrieve.py`'s own `_document` helper: retrieval now
    filters to `status = 'ready'` (Phase 3 debt 1), so every document a
    test seeds chunks onto directly must be marked ready or nothing it
    seeds will ever be retrievable."""
    document = await DocumentService(session, tenant).create(
        CreateDocumentInput(title=title, source_type=DocumentSourceType.TEXT)
    )
    await DocumentService(session, tenant).mark_ready(document.id)
    return document


async def _seed(session, tenant, document_id, entries: list[tuple[str, list[float]]]) -> None:
    chunks = [
        ChunkInput(
            content=content,
            token_count=len(content.split()),
            embedding=embedding,
            embedding_model="hashing",
        )
        for content, embedding in entries
    ]
    await DocumentService(session, tenant).replace_chunks(document_id, chunks)


def _ctx(tenant, **overrides) -> ToolContext:
    payload: dict[str, object] = {
        "organization_id": tenant.organization_id,
        "agent_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ToolContext(**payload)


async def test_relevant_query_returns_the_matching_chunk_with_citations(tenant_a):
    query = "refund policy thirty days"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a, title="Policies")
        await _seed(
            session,
            tenant_a,
            document.id,
            [("Our refund policy allows a full refund within thirty days.", query_vector)],
        )

    async with tenant_session(tenant_a) as session:
        tool = RetrieveKnowledgeTool(session, embedder=_embedder)
        result = await tool.execute(RetrieveKnowledgeTool.args_model(query=query), _ctx(tenant_a))

    assert result.is_error is False
    assert "refund policy" in result.content
    assert len(result.citations) == 1
    assert result.citations[0].document_id == document.id
    assert "refund" in result.citations[0].excerpt


async def test_irrelevant_query_returns_the_explicit_no_results_message(tenant_a):
    """§5.2's specific mechanism against a model papering over ignorance:
    an empty result must read as an explicit statement that nothing was
    found, never an empty string or an empty citations list with no
    accompanying text a model could mistake for "no need to mention this".

    The chunk's embedding is the query vector's exact negation (cosine
    distance exactly 2.0, deterministic under any embedder) rather than an
    embedding of unrelated text, so this cannot pass by accident of
    `HashingEmbedder`'s bag-of-words hash happening to land the two close
    together -- the corpus also shares no vocabulary with the query, so
    neither retrieval arm has anything to offer.
    """
    query = "what is the weather in tokyo tomorrow"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                (
                    "Support hours are Monday through Friday, nine to five.",
                    _negate(query_vector),
                )
            ],
        )

    async with tenant_session(tenant_a) as session:
        tool = RetrieveKnowledgeTool(session, embedder=_embedder)
        result = await tool.execute(
            RetrieveKnowledgeTool.args_model(query=query),
            _ctx(tenant_a),
        )

    assert result.is_error is False
    assert result.content == _NO_RESULTS_MESSAGE
    assert result.content != ""
    assert result.citations == []


async def test_top_k_is_clamped_to_a_sane_maximum(tenant_a):
    """A model can ask for `top_k=1000` -- nothing about the tool schema
    stops it, since `args_model` only knows it is an `int`. The corpus here
    seeds more chunks than `_MAX_TOP_K` (all at the query's own vector, so
    every one of them clears the default relevance floor and is a genuine
    fusion candidate, not merely padding), so an unclamped implementation
    would return more than `_MAX_TOP_K` results and this test would catch
    it; a version that clamped to something looser than `_MAX_TOP_K` would
    also be caught, since the corpus is sized specifically one past it.
    """
    query = "unique proprietary ingredient formulation"
    query_vector = await _embed(query)
    corpus_size = _MAX_TOP_K + 5

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        entries = [
            (f"Filler passage number {i} about nothing in particular.", query_vector)
            for i in range(corpus_size)
        ]
        await _seed(session, tenant_a, document.id, entries)

    async with tenant_session(tenant_a) as session:
        tool = RetrieveKnowledgeTool(session, embedder=_embedder)
        result = await tool.execute(
            RetrieveKnowledgeTool.args_model(query=query, top_k=1000), _ctx(tenant_a)
        )

    assert len(result.citations) == _MAX_TOP_K


async def test_cross_tenant_isolation_via_tool_context(tenant_a, tenant_b, owner_connection):
    """The tool takes tenancy only from `ToolContext.organization_id` --
    there is no argument through which a model could ask for another
    tenant's data (`ToolRegistry.register` would refuse to register a tool
    whose `args_model` even exposed one). Org B's chunk is seeded as the
    strongest possible lexical AND vector match for org A's query, so a
    version that forgot to scope by `ctx.organization_id` -- or that took an
    org id from somewhere other than `ToolContext` -- would surface it, and
    a weaker corpus (no vocabulary overlap) would pass even with zero
    isolation.
    """
    query = "extended service agreement coverage terms"
    query_vector = await _embed(query)
    dominant_match = " ".join([query] * 10)

    async with tenant_session(tenant_b) as session:
        other_document = await _document(session, tenant_b, title="Org B doc")
        await _seed(session, tenant_b, other_document.id, [(dominant_match, query_vector)])

    async with tenant_session(tenant_a) as session:
        own_document = await _document(session, tenant_a, title="Org A doc")
        await _seed(
            session, tenant_a, own_document.id, [(f"{query} is described briefly.", query_vector)]
        )

    async with tenant_session(tenant_a) as session:
        tool = RetrieveKnowledgeTool(session, embedder=_embedder)
        result = await tool.execute(RetrieveKnowledgeTool.args_model(query=query), _ctx(tenant_a))

    assert len(result.citations) >= 1
    assert all(c.document_id == own_document.id for c in result.citations)

    # Confirm org B's chunk really exists and really would have won the
    # keyword race, so this passing means isolation worked -- not that the
    # chunk never existed to begin with.
    other_chunk_id = (
        await owner_connection.execute(
            text("SELECT id FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": other_document.id},
        )
    ).scalar_one()
    assert other_chunk_id not in {c.chunk_id for c in result.citations}


async def test_sql_level_failure_does_not_poison_the_callers_transaction(tenant_a, monkeypatch):
    """The savepoint ruling, restated for this one layer: Phase 3's review
    found `RetrievalService.retrieve` running raw SQL directly on the
    caller's session, so a database-level failure aborted the whole turn's
    transaction. That risk has moved, not gone -- this tool runs the same
    SQL one layer further from whoever owns the transaction (here,
    `tenant_session`, which the real chat turn's transaction will stand in
    for). This test injects a genuine SQL-level failure -- a statement
    Postgres itself rejects -- on the *real* session, not a bare
    `RuntimeError`: a `RuntimeError` raised from a method that never
    touches the session would leave the transaction perfectly healthy and
    pass even over an implementation with no savepoint at all, which is
    exactly how Phase 3 proved that class of test cannot catch this bug.

    Routed through `ToolRegistry.execute`, not `tool.execute` directly, so
    this also confirms the tool leaves the *raised* exception to propagate
    rather than swallowing it itself -- `ToolRegistry.execute` is what turns
    it into `ToolResult(is_error=True)`, and re-catching it in the tool too
    would just duplicate that, which is exactly why the brief says not to
    re-handle what the registry already handles.
    """

    async def _broken_retrieve(self, query, **kwargs):
        # A statement Postgres rejects outright (an unknown table), executed
        # on the real session passed into the tool -- not a mock, not a
        # method that never touches the database at all.
        await self.session.execute(text("SELECT * FROM this_table_does_not_exist_at_all"))
        raise AssertionError("unreachable: the statement above must raise first")

    monkeypatch.setattr(RetrievalService, "retrieve", _broken_retrieve)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [("Ordinary content, unrelated to the failure being injected.", await _embed("x"))],
        )

    async with tenant_session(tenant_a) as session:
        registry = ToolRegistry()
        registry.register(RetrieveKnowledgeTool(session, embedder=_embedder))

        call = ToolUseBlock(id="call_1", name="retrieve_knowledge", input={"query": "anything"})
        result = await registry.execute(call, _ctx(tenant_a))

        assert result.is_error is True

        # The transaction must still be usable after the failure -- a
        # poisoned transaction would make this raise `InFailedSqlTransaction
        # Error` (surfaced through SQLAlchemy as a `DBAPIError`) rather than
        # returning a row, proving the savepoint rolled back only the
        # failing statement, not everything after it.
        try:
            probe = await session.execute(text("SELECT 1"))
        except DBAPIError:
            pytest.fail("the caller's transaction was left poisoned by the tool's DB failure")
        assert probe.scalar_one() == 1

        # The strongest form of "still usable": the same session, same
        # transaction, can run the tool again and get a real answer -- not
        # merely execute an unrelated SELECT.
        monkeypatch.undo()
        second_call = ToolUseBlock(
            id="call_2", name="retrieve_knowledge", input={"query": "ordinary content"}
        )
        second_result = await registry.execute(second_call, _ctx(tenant_a))
        assert second_result.is_error is False
        assert len(second_result.citations) == 1
