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
from app.rag.ingest import ingest_document
from app.rag.retrieve import RetrievalService

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


def _negate(vector: list[float]) -> list[float]:
    return [-v for v in vector]


async def _document(session, tenant, title: str = "Doc"):
    """Every test in this file that seeds chunks directly (as opposed to
    running the real `ingest_document` pipeline, which sets this status
    itself) needs its document at `status=ready` -- retrieval now filters
    to it (see the `d.status = 'ready'` predicate in `app/rag/retrieve.py`,
    Phase 3's first carried debt). Marking it ready here, once, keeps every
    other test in this file about the *retrieval* behaviour it was written
    to test rather than about document lifecycle plumbing it never asked
    to depend on.
    """
    document = await DocumentService(session, tenant).create(
        CreateDocumentInput(title=title, source_type=DocumentSourceType.TEXT)
    )
    await DocumentService(session, tenant).mark_ready(document.id)
    return document


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
        # `max_distance=2.0` disables the vector arm's relevance floor
        # for this test. The corpus here is built from negated query
        # vectors (cosine distance exactly 2), which is how it makes
        # "near" and "far" deterministic -- a deliberately synthetic
        # distance no real text produces, and one the default floor of
        # 0.8 would simply filter out, leaving nothing to order. What
        # the floor itself does is asserted on a real corpus further
        # down this file.
        results = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=2, max_distance=2.0
        )

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
        # `max_distance=2.0` disables the vector arm's relevance floor
        # for this test. The corpus here is built from negated query
        # vectors (cosine distance exactly 2), which is how it makes
        # "near" and "far" deterministic -- a deliberately synthetic
        # distance no real text produces, and one the default floor of
        # 0.8 would simply filter out, leaving nothing to order. What
        # the floor itself does is asserted on a real corpus further
        # down this file.
        results = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=2, max_distance=2.0
        )

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
        # `max_distance=2.0` disables the vector arm's relevance floor
        # for this test. The corpus here is built from negated query
        # vectors (cosine distance exactly 2), which is how it makes
        # "near" and "far" deterministic -- a deliberately synthetic
        # distance no real text produces, and one the default floor of
        # 0.8 would simply filter out, leaving nothing to order. What
        # the floor itself does is asserted on a real corpus further
        # down this file.
        top_limited = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=2, max_distance=2.0
        )
    assert len(top_limited) == 2

    async with tenant_session(tenant_a) as session:
        unfiltered = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=5, max_distance=2.0
        )
    assert len(unfiltered) == 5
    weakest_score = min(r.score for r in unfiltered)
    strongest_score = max(r.score for r in unfiltered)
    assert weakest_score < strongest_score

    async with tenant_session(tenant_a) as session:
        filtered = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=5, min_score=weakest_score + 1e-9, max_distance=2.0
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


async def test_organization_id_predicate_holds_even_when_rls_is_bypassed(tenant_a, tenant_b):
    """`docs/ARCHITECTURE.md` section 2.3's two-layer tenancy: RLS (layer 2)
    is not the only thing standing between this query and another org's
    rows. Every other test in this file exercises retrieval on an ordinary
    RLS-scoped session, where layer 2 alone would already stop a leak --
    which cannot tell layer 1 (the explicit `organization_id` predicate in
    `retrieve.py`'s SQL) apart from layer 1 not existing at all.

    This test removes layer 2 entirely: `unscoped_session` runs as
    `app_owner`, the migration role that bypasses RLS (the same role
    `owner_connection` uses elsewhere in this suite), with no
    `app.current_org_id` ever set on it. A *non*-bypassing but merely
    unscoped session would not serve this purpose -- RLS's own
    `NULLIF(current_setting(...), '')::uuid` guard makes an unset org id
    fail closed to zero rows under RLS, which would pass this assertion
    even with the predicate deleted from `retrieve.py`. Only a genuinely
    RLS-bypassing session isolates layer 1: if the explicit predicate were
    ever removed, this specific test -- and only this one -- would start
    returning org B's chunk.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    query = "extended vehicle protection plan details"
    query_vector = await _embed(query)

    async with tenant_session(tenant_b) as session:
        other_document = await _document(session, tenant_b, title="Org B doc")
        await _seed(session, tenant_b, other_document.id, [(query, query_vector)])

    async with tenant_session(tenant_a) as session:
        own_document = await _document(session, tenant_a, title="Org A doc")
        await _seed(session, tenant_a, own_document.id, [(query, query_vector)])

    unscoped_engine = create_async_engine(get_settings().migration_database_url)
    try:
        unscoped_session_factory = async_sessionmaker(unscoped_engine, expire_on_commit=False)
        async with unscoped_session_factory() as unscoped_session:
            results = await RetrievalService(unscoped_session, tenant_a).retrieve(query, top_k=5)
    finally:
        await unscoped_engine.dispose()

    assert len(results) >= 1
    assert all(r.document_id == own_document.id for r in results)


async def test_rrf_score_matches_the_literal_1_based_formula(tenant_a):
    """Ranks feeding into RRF are 1-based (`enumerate(rows, start=1)`), per
    the published formula and per this module's own docstring/comment --
    but nothing else in this file would catch a regression to 0-based
    ranks: shifting every rank down by one changes every fused score by
    roughly the same proportion and flips no *pairwise* ordering the other
    tests check (confirmed: mutating `start=1` to `start=0` leaves every
    other test in this file green).

    This pins an absolute value instead. `target` is vector rank 2
    (distance > 0, `decoy` takes rank 1) and the sole keyword match (rank
    1). Under 1-based ranks its fused score is exactly
    `1/(60+2) + 1/(60+1)`; under 0-based ranks it would be
    `1/(60+1) + 1/(60+0)` instead -- a different value, not just a
    different ordering.
    """
    query = "annual roadside assistance coverage"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                (
                    # Vector rank 1 (identical to the query vector), no
                    # shared vocabulary with the query -> absent from the
                    # keyword list.
                    "The quarterly sales meeting starts promptly every Monday morning downtown.",
                    query_vector,
                ),
                (
                    # Vector rank 2 (far from the query vector), the only
                    # chunk containing the query's own words -> keyword
                    # rank 1.
                    "Annual roadside assistance coverage applies to eligible vehicles.",
                    _negate(query_vector),
                ),
            ],
        )

    async with tenant_session(tenant_a) as session:
        # `max_distance=2.0` disables the vector arm's relevance floor
        # for this test. The corpus here is built from negated query
        # vectors (cosine distance exactly 2), which is how it makes
        # "near" and "far" deterministic -- a deliberately synthetic
        # distance no real text produces, and one the default floor of
        # 0.8 would simply filter out, leaving nothing to order. What
        # the floor itself does is asserted on a real corpus further
        # down this file.
        results = await RetrievalService(session, tenant_a).retrieve(
            query, top_k=2, max_distance=2.0
        )

    target = next(r for r in results if "roadside" in r.content)
    expected_score = 1.0 / (60 + 2) + 1.0 / (60 + 1)
    assert target.score == pytest.approx(expected_score)


async def test_empty_corpus_returns_empty_list_rather_than_raising(tenant_a):
    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve("anything at all")

    assert results == []


# A small, real, multi-topic corpus. Unlike the seeded (content, vector)
# pairs above, every chunk here is embedded from its *own* text, so the
# distances between a query and these chunks are the distances the shipped
# embedder actually produces -- which is the only way to say anything
# meaningful about a relevance threshold.
_HANDBOOK = [
    "The powertrain warranty covers the engine, transmission and drive axles for "
    "five years or sixty thousand miles, whichever comes first.",
    "Qualified buyers may finance a new vehicle at rates starting from 3.9 percent "
    "APR over sixty months. A trade-in appraisal is free.",
    "The cabin air filter is replaced every fifteen thousand miles. Engine oil and "
    "the oil filter are changed every ten thousand miles.",
    "You have thirty days to file a return on any accessory purchase. Returns filed "
    "after that window are handled case by case.",
]


async def _seed_handbook(session, tenant) -> uuid.UUID:  # type: ignore[no-untyped-def]
    document = await _document(session, tenant, title="Owner's handbook")
    entries = [(content, await _embed(content)) for content in _HANDBOOK]
    await _seed(session, tenant, document.id, entries)
    return document.id


@pytest.mark.parametrize(
    "query",
    ["hi", "thanks!", "what is the weather in tokyo tomorrow", "does it come in red?"],
)
async def test_an_unrelated_query_retrieves_nothing_at_all(tenant_a, query):
    """The relevance floor, from the side that had none at all.

    Fused RRF scores are derived from rank position: the top hit is exactly
    `1/61` whatever it contains, so no threshold applied *after* fusion can
    express relevance. With the vector retriever returning its nearest
    `candidates` rows unconditionally, every single turn -- `hi` included --
    put up to `top_k` chunks into the system prompt, onto the user's
    "Sources" list, and into `message_citations` as having grounded the
    answer.

    Measured against this corpus with the shipped `HashingEmbedder`, these
    four queries sit at cosine distance 0.82-1.00 from their nearest chunk,
    while every question the handbook actually answers sits at 0.60-0.71
    (see the test below). `does it come in red?` is the interesting one: it
    shares the stemmed lexeme `come` with the warranty chunk's "comes
    first", so it is the case a keyword arm alone would still cite.
    """
    async with tenant_session(tenant_a) as session:
        await _seed_handbook(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query)

    assert results == []


@pytest.mark.parametrize(
    ("query", "expected_fragment"),
    [
        ("how long is the powertrain warranty", "powertrain warranty"),
        ("when is the cabin air filter replaced", "cabin air filter"),
        ("How long do I have to file a return?", "file a return"),
        ("what interest rate can I get on financing", "finance a new vehicle"),
    ],
)
async def test_a_relevant_question_still_retrieves_its_own_section(
    tenant_a, query, expected_fragment
):
    """The other half of the floor: it must not silence real questions.

    Each of these is a whole natural-language question, the shape a chat
    product actually receives, and each must still rank its own section
    first. `what interest rate can I get on financing` is deliberately in
    the list: it is the one whose *vector* distance (0.94) is above the
    ceiling, so it survives only through the keyword arm -- which is the
    hybrid earning its keep, and would break if the floor were applied
    after fusion instead of per arm.
    """
    async with tenant_session(tenant_a) as session:
        await _seed_handbook(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query)

    assert results, f"{query!r} retrieved nothing"
    assert expected_fragment in results[0].content


@pytest.mark.parametrize(
    ("query", "expected_fragment"),
    [
        # The strict `websearch_to_tsquery` AND form matches nothing for
        # these two -- 'long' & 'file' & 'return' and 'powertrain' &
        # 'warranti' & 'cover' & 'exact' both carry a word the corpus does
        # not have -- so only the OR fallback can answer them.
        ("How long do I have to file a return?", "file a return"),
        ("What does the powertrain warranty cover exactly?", "powertrain warranty"),
        # These two the strict form already matched, and must keep matching:
        # the fallback runs only when the strict form found nothing.
        ("file a return", "file a return"),
        ("returns filed", "file a return"),
    ],
)
async def test_the_keyword_arm_answers_a_conversational_question(
    tenant_a, query, expected_fragment
):
    """`max_distance=-1.0` switches the vector arm off entirely (cosine
    distance is never negative), so what this asserts is the keyword arm
    alone -- otherwise the vector arm would answer and hide the fact that
    the lexical half had gone silent, which is exactly how this survived
    eight task reviews.

    The keyword arm is the only one that stems: `HashingEmbedder` hashes
    raw tokens, so "return" and "returns" are unrelated to it. A silent
    keyword arm therefore does not merely cost a second opinion, it costs
    the stemming that `docs/PHASE-3.md` §4's argument for hybrid search
    depends on.
    """
    async with tenant_session(tenant_a) as session:
        await _seed_handbook(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve(query, max_distance=-1.0)

    assert results, f"{query!r} retrieved nothing from the keyword arm"
    assert expected_fragment in results[0].content


# A document as bytes, not as pre-built chunks: this one goes through
# `extract` -> `chunk_document` -> the real embedder -> `replace_chunks`,
# which is the only path in this suite where the *corpus* is embedded by
# the shipped embedder rather than seeded from the query's own vector.
# Higher than any `ts_rank_cd` a real match produces, so passing it as
# `min_keyword_rank` suppresses the keyword arm's OR fallback entirely --
# which, combined with a query whose strict AND form matches nothing, is how
# a test isolates the vector arm.
_KEYWORD_ARM_OFF = 1e9

_HANDBOOK_MARKDOWN = (
    b"# Warranty\n\n"
    b"The powertrain warranty covers the engine, transmission and drive axles "
    b"for five years or sixty thousand miles, whichever comes first. Corrosion "
    b"perforation is covered separately for seven years with no mileage limit.\n\n"
    b"# Financing\n\n"
    b"Qualified buyers may finance a new vehicle at rates starting from 3.9 "
    b"percent APR over sixty months. A trade-in appraisal is free and takes "
    b"about twenty minutes at any dealership.\n\n"
    b"# Service intervals\n\n"
    b"The cabin air filter is replaced every fifteen thousand miles. Engine oil "
    b"and the oil filter are changed every ten thousand miles under normal "
    b"driving conditions, or every five thousand under severe use.\n"
)


async def test_a_document_ingested_through_the_real_embedder_retrieves_its_own_section(tenant_a):
    """The one test that pins `docs/PHASE-3.md` §2.2's load-bearing claim at
    system level: that the default embedder is real, rather than a fake that
    hashes each input to a random vector, *because* a fake would make every
    downstream retrieval test pass without testing anything.

    Every other retrieval test in this file embeds the query for real and
    then seeds each chunk's vector as a copy or a negation of it, so the
    corpus never passes through the embedder at all. Replacing
    `HashingEmbedder._embed_one` with seeded Gaussian noise left all of them
    green -- the prophecy in §2.2, true of the suite that was supposed to
    prevent it.

    Two halves, and the second is the one that does the pinning:

    1. the whole pipeline, everything on: a real document ingested through
       `ingest_document` (real `extract`, real chunking, real embeddings),
       queried with ordinary English questions that share no exact phrasing
       with the sections answering them, each ranking its own section first.
    2. the *vector arm alone*. Half 1 on its own is not enough, and finding
       that out is worth recording: under a noise embedder the vector arm
       returns nothing at all (random unit vectors sit at cosine distance
       ~1.0, past the relevance floor), and the keyword arm then answers all
       three queries correctly by itself -- green suite, dead embedder,
       exactly the vacuity this test exists to prevent. So each query below
       is first shown to find *nothing* with the vector arm switched off
       (`max_distance=-1.0`) and the keyword fallback suppressed
       (`min_keyword_rank`), which proves the strict keyword form does not
       match it; whatever the same query then returns with the vector arm
       switched back on can only have come from the embedding.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a, title="Owner's handbook")

    async with tenant_session(tenant_a) as session:
        result = await ingest_document(
            session, tenant_a, document.id, _HANDBOOK_MARKDOWN, "text/markdown"
        )

    # One chunk per heading section -- if chunking ever merges them, the
    # ranking assertions below stop meaning what they say.
    assert result.chunk_count == 3
    assert result.embedding_model == "hashing"

    for query, expected_fragment in [
        ("how long is the powertrain warranty", "powertrain warranty"),
        ("how often should the cabin air filter be changed", "cabin air filter"),
        ("what interest rate can I get on financing", "finance a new vehicle"),
    ]:
        async with tenant_session(tenant_a) as session:
            results = await RetrievalService(session, tenant_a).retrieve(query)
        assert results, f"{query!r} retrieved nothing"
        assert expected_fragment in results[0].content, (
            f"{query!r} ranked {results[0].content[:60]!r} first"
        )

    # `interest rate ... financing` is deliberately absent here: its section
    # sits at cosine distance 0.94, past the floor, so the keyword arm is
    # what finds it (see the conversational-query test above). The other two
    # sit at 0.62 and 0.65, and their strict keyword form matches nothing --
    # asserted, not assumed, by the first call in each pair.
    for query, expected_fragment in [
        ("how long is the powertrain warranty", "powertrain warranty"),
        ("how often should the cabin air filter be changed", "cabin air filter"),
    ]:
        async with tenant_session(tenant_a) as session:
            service = RetrievalService(session, tenant_a)
            keyword_only = await service.retrieve(
                query, max_distance=-1.0, min_keyword_rank=_KEYWORD_ARM_OFF
            )
            vector_only = await service.retrieve(query, min_keyword_rank=_KEYWORD_ARM_OFF)

        assert keyword_only == [], f"{query!r} was answerable without the embedder"
        assert vector_only, f"{query!r} retrieved nothing from the vector arm"
        assert expected_fragment in vector_only[0].content


# --- Phase 3 debt 1: `documents.status` must gate retrieval -----------------


async def test_stale_chunks_of_a_document_now_failed_do_not_surface(tenant_a):
    """Reproduces, live, exactly what Phase 3's final review found: a
    document ingests to `ready` (chunks written, query answerable), a later
    re-ingest fails during extraction *before* `replace_chunks` ever runs,
    so `documents.status` flips to `failed` while the old chunks -- still
    attached to that same `document_id` -- are left untouched in
    `document_chunks`. Without `d.status = 'ready'` in both retrieval arms,
    those stale rows keep grounding answers forever, even though the
    dashboard has already told the user this document failed.

    The positive-control retrieval (while still `ready`) is load-bearing,
    not decorative: without it, a passing assertion after `mark_failed`
    could just as easily mean the corpus never matched this query at all,
    which is the vacuous shape this suite keeps guarding against.
    """
    query = "extended cargo warranty terms for pickup beds"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a, title="Cargo terms")
        await _seed(
            session,
            tenant_a,
            document.id,
            [("Extended cargo warranty terms cover beds, racks and tie-downs.", query_vector)],
        )

    async with tenant_session(tenant_a) as session:
        while_ready = await RetrievalService(session, tenant_a).retrieve(query)
    assert while_ready, "the seeded chunk should answer this query while the document is ready"

    async with tenant_session(tenant_a) as session:
        await DocumentService(session, tenant_a).mark_failed(document.id, "extraction crashed")

    async with tenant_session(tenant_a) as session:
        after_failed = await RetrievalService(session, tenant_a).retrieve(query)

    assert after_failed == []


# --- Phase 3 debt 2: a bare negation must not match the whole corpus --------


async def test_bare_leading_hyphen_query_does_not_match_the_whole_corpus(tenant_a):
    """`websearch_to_tsquery` reads a leading hyphen as negation, so a query
    of just `-cat` parses to `!'cat'`, which `@@` matches against every
    chunk that merely lacks the word "cat" -- the entire corpus below, none
    of which mentions it. Before the strict (AND) keyword arm carried a
    rank floor, that satisfied its `WHERE` clause outright (`ts_rank_cd`
    scores every one of those matches exactly 0.0, but nothing checked it),
    so all three chunks would come back cited for a query that named no
    positive term at all -- and the OR-fallback path, which *does* have a
    floor, never even ran, because the strict form did not return zero
    rows.

    The vector arm is switched off (`max_distance=-1.0`, never a valid
    cosine distance) so this isolates the keyword arm's own behaviour --
    otherwise a coincidental vector-arm hit could paper over a still-broken
    keyword floor.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
        await _seed(
            session,
            tenant_a,
            document.id,
            [
                (
                    "The quarterly sales report is due at the end of the month.",
                    await _embed("unrelated filler one"),
                ),
                (
                    "Employees may request remote work with manager approval.",
                    await _embed("unrelated filler two"),
                ),
                (
                    "The parking garage closes at midnight on weekdays.",
                    await _embed("unrelated filler three"),
                ),
            ],
        )

    async with tenant_session(tenant_a) as session:
        results = await RetrievalService(session, tenant_a).retrieve("-cat", max_distance=-1.0)

    assert results == []
