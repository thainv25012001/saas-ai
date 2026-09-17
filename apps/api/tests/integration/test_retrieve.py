"""Hybrid retrieval: vector similarity + keyword search, fused with RRF.

Every test seeds `DocumentChunk` rows directly through `DocumentService.
replace_chunks` rather than running the full ingestion pipeline -- what
matters here is the *shape* of the corpus (which chunk is the best vector
match, which is the best keyword match, which is both), not extraction or
chunking. Content and embedding are set independently per chunk: content
drives `content_tsv`/keyword matching, the embedding field is whatever
vector each test needs it to be. That decoupling is what makes it possible
to build a corpus where the wrong answer is a plausible competitor, per the
brief -- e.g. a chunk that is the single best vector match yet must still
lose the fused ranking to a chunk found by both retrievers (test 3).

`HashingEmbedder` (the default, no network call) is used directly to embed
query text into a real 1536-dim vector, so "near" and "far" chunks can be
built by copying or negating that vector -- deterministic distances of
(about) 0 and 2 under cosine distance, rather than hoping unrelated text
happens to land far apart.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.tenancy import tenant_session
from app.db.models import DocumentSourceType
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.rag.retrieve import RetrievalService

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


def _negate(vector: list[float]) -> list[float]:
    return [-v for v in vector]


async def _document(session, tenant, title: str = "Doc"):
    return await DocumentService(session, tenant).create(
        CreateDocumentInput(title=title, source_type=DocumentSourceType.TEXT)
    )


async def _seed(
    session, tenant, document_id: uuid.UUID, entries: list[tuple[str, list[float]]]
) -> None:
    """Write chunks with explicit (content, embedding) pairs, in order."""
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


async def test_keyword_match_ranks_above_a_chunk_with_no_shared_vocabulary(tenant_a):
    query = "refund policy thirty days"
    query_vector = await _embed(query)
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        # Both chunks get the *same* embedding (the query's own vector), so
        # vector search alone cannot distinguish them -- only the keyword
        # retriever's WHERE/rank can be responsible for the ordering this
        # test asserts. A version that ignored content_tsv entirely would
        # still pass a test where the relevant chunk also happened to have
        # the closer embedding; giving both chunks the identical vector
        # closes that loophole.
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                (
                    "Our refund policy allows a full refund within thirty days of purchase.",
                    query_vector,
                ),
                (
                    "Support hours are Monday through Friday, nine to five.",
                    query_vector,
                ),
            ],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, top_k=2)

    assert len(results) == 2
    assert "refund" in results[0].content
    assert results[0].score > results[1].score


async def test_smaller_cosine_distance_ranks_first(tenant_a):
    """Pins the `<=>` direction. `<=>` is cosine *distance*; ordering by it
    ascending (closest first) is correct. Reversing the ORDER BY direction
    (or otherwise treating the raw `<=>` value as a similarity) would make
    `far` outrank `near` here, and this is the only assertion in the suite
    built to catch exactly that -- both chunks are lexically identical
    filler with no keyword overlap with the query, so RRF has nothing to
    fuse and the final order is pure vector order.
    """
    query = "widget assembly instructions"
    query_vector = await _embed(query)
    near = query_vector
    far = _negate(query_vector)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                ("Completely unrelated filler content about nothing in particular.", far),
                ("Completely unrelated filler content about nothing in particular too.", near),
            ],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, top_k=2)

    assert len(results) == 2
    assert results[0].content.endswith("too.")
    assert results[0].score > results[1].score


async def test_chunk_found_by_both_retrievers_outranks_the_single_best_vector_match(tenant_a):
    """The strong form of the brief's test 3: `vector_best` has the single
    best individual placement of any chunk in the corpus (vector rank 1,
    distance exactly 0) yet must still lose to `both`, which is merely
    vector rank 2 but *also* the sole keyword match. This is only possible
    under genuine RRF summation across lists (`1/61 + 1/62 > 1/61`); a bug
    that took each chunk's single best rank instead of summing across the
    lists it appears in would rank `vector_best` first, and this test would
    catch it where an "equal ranks" version could not (that weaker version
    is exactly what a max-instead-of-sum bug would still pass).
    """
    query = "premium warranty manufacturing defects"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                (
                    # Vector rank 1 (identical to the query vector), zero
                    # shared vocabulary with the query -> absent from the
                    # keyword list entirely.
                    "The quarterly sales meeting starts promptly every Monday morning downtown.",
                    query_vector,
                ),
                (
                    # Vector rank 2 (deliberately far from the query
                    # vector), but the only chunk containing the query's
                    # own words -> keyword rank 1.
                    "Our premium warranty covers manufacturing defects for two years "
                    "from purchase.",
                    _negate(query_vector),
                ),
            ],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, top_k=2)

    assert len(results) == 2
    assert "premium warranty" in results[0].content
    assert results[0].score > results[1].score


async def test_keyword_only_match_surfaces_despite_losing_on_vector_alone(tenant_a):
    """A chunk that is lexically exact but embeds nowhere near the query
    (simulating a rare token the hashing embedder's bag-of-words buckets
    away from anything resembling the query's own vector) must still
    surface -- keyword search has no candidate cutoff working against it
    here because `candidates=2` locks it out of the vector half entirely:
    two decoys are strictly closer to the query vector than it is, so it
    never appears in the vector list at all, and only the keyword path can
    account for it showing up in the fused result.
    """
    query = "zylofrantic warranty clause"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                ("Decoy chunk one, close to the query vector.", query_vector),
                ("Decoy chunk two, also close to the query vector.", query_vector),
                (
                    "This warranty clause references the rare term zylofrantic explicitly.",
                    _negate(query_vector),
                ),
            ],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, candidates=2, top_k=3)

    assert any("zylofrantic" in r.content for r in results)


async def test_malformed_query_syntax_does_not_raise(tenant_a):
    """`websearch_to_tsquery` tolerates stray `&`/`:`/quotes the way
    `to_tsquery` does not -- this input would raise a syntax error from
    Postgres under `to_tsquery` and surface as an unhandled 500."""
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [("Warranty claims are honored for 30 days.", await _embed("warranty claims"))],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(
            'warranty & "claims" : 30 days'
        )

    assert isinstance(results, list)


async def test_top_k_is_respected_and_min_score_filters(tenant_a):
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        entries = [
            (f"Annual maintenance inspection checklist item number {i}.", query_vector)
            for i in range(4)
        ]
        # A fifth chunk, far from the query and with no shared vocabulary --
        # the weakest possible fused score, present only via the vector
        # list at the worst rank.
        entries.append(
            ("Totally unrelated filler paragraph about gardening.", _negate(query_vector))
        )
        await _seed(session, tenant_a, document.id, entries)

    async with tenant_session(tenant_a) as session:
        top_limited = await RetrievalService(session, tenant_a).retrieve(query, top_k=2)
    assert len(top_limited) == 2

    async with tenant_session(tenant_a) as session:
        unfiltered = await RetrievalService(session, tenant_a).retrieve(query, top_k=5)
    assert len(unfiltered) == 5
    weakest_score = min(r.score for r in unfiltered)
    strongest_score = max(r.score for r in unfiltered)
    assert weakest_score < strongest_score

    async with tenant_session(tenant_a) as session:
        filtered = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=5, min_score=weakest_score + 1e-9
        )
    assert len(filtered) == len(unfiltered) - 1
    assert all(r.score >= weakest_score + 1e-9 for r in filtered)


async def test_cross_tenant_chunk_never_surfaces_even_as_the_best_lexical_match(
    tenant_a, tenant_b, owner_connection
):
    """Org B's chunk is seeded as the *strongest possible* lexical match for
    org A's query -- many repetitions of the exact query text, which
    `ts_rank_cd` rewards directly. If retrieval only filtered results after
    the fact (or not at all) rather than scoping the underlying queries to
    the tenant session's RLS, this chunk would win the keyword race outright
    and this test would catch it. A weaker version -- org B's chunk sharing
    no vocabulary with org A's query -- would pass even with zero isolation,
    which is the vacuous shape this suite is trying to avoid.
    """
    query = "extended service agreement coverage terms"
    query_vector = await _embed(query)
    dominant_match = " ".join([query] * 10)

    async with tenant_session(tenant_b) as session:
        other_document = await _document(session, tenant_b, title="Org B doc")
        await _seed(
            session,
            tenant_b,
            other_document.id,
            [(dominant_match, query_vector)],
        )

    async with tenant_session(tenant_a) as session:
        own_document = await _document(session, tenant_a, title="Org A doc")
        await _seed(
            session,
            tenant_a,
            own_document.id,
            [(f"{query} is described briefly here.", query_vector)],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, top_k=5)

    assert len(results) >= 1
    assert all(r.document_id == own_document.id for r in results)

    # Confirm org B's chunk really is in the database and really would have
    # won the keyword race, so a passing test above means isolation worked
    # -- not that the chunk never existed.
    other_chunk_id = (
        await owner_connection.execute(
            text("SELECT id FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": other_document.id},
        )
    ).scalar_one()
    assert other_chunk_id not in {r.chunk_id for r in results}


async def test_empty_corpus_returns_empty_list_rather_than_raising(tenant_a):
    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve("anything at all")

    assert results == []
