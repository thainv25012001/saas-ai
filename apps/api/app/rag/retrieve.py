"""Hybrid retrieval: vector similarity + keyword search, fused with RRF.

Task 7 (chat prompt assembly) consumes this module's output as the
passages a response gets to cite; nothing upstream of this file scores
relevance, so getting the fusion and the tenancy scoping right here is the
whole point of the phase.

Tenancy here follows `docs/ARCHITECTURE.md` section 2.3's two layers, same as
every other service in this codebase (see the explicit `organization_id`
filters in `app/documents/service.py`): Layer 2 is RLS on the caller's
tenant-bound session, and Layer 1 is the explicit `organization_id`
predicate both queries below carry regardless. RLS alone would have been
enough for any session this module is actually ever called with in this
codebase today -- but `AsyncSession`'s type carries no proof of that, and
nothing stops a future caller (a script, a background job wired up wrong)
from handing this an owner-privileged, RLS-bypassing session. The explicit
predicate is what still fails closed to zero cross-tenant rows in that
case rather than depending entirely on the caller's session having been
built correctly.

Two independent candidate lists are pulled per query:

- vector: nearest neighbours by `embedding <=> :query_vector` (pgvector's
  cosine *distance* operator -- smaller is more similar), ascending.
- keyword: `content_tsv @@ websearch_to_tsquery('english', :query_text)`,
  ordered by `ts_rank_cd` descending, with an OR-joined fallback when that
  strict form matches nothing (see `_AND_TSQUERY`/`_OR_TSQUERY` below).
  `websearch_to_tsquery` specifically, not `to_tsquery`: the latter raises
  on bare `&`/`|`/`:`/unbalanced quotes in the input, which for a query
  built from a customer's own words is a matter of when, not if.

The two lists are combined with Reciprocal Rank Fusion rather than trying
to make cosine distance and `ts_rank_cd` commensurable -- they are on
unrelated scales and neither is a probability, so there is no principled
way to add them directly. RRF sidesteps that by fusing on rank position
alone.

That last property is exactly why the relevance floor lives on each arm
rather than on the fused score. An RRF score carries no information about
*how good* a match is -- the top hit is `1/61` whether it answers the
question or is the least-bad row in an unrelated corpus -- so a
`min_score` applied after fusion can only ever mean "appeared in one list"
versus "appeared in both". Before this floor existed the vector arm
returned its nearest `candidates` rows unconditionally, and a corpus with
any documents in it therefore cited up to `top_k` chunks on *every* turn,
`hi` included: into the system prompt, onto the user's "Sources" list, and
into `message_citations` as having grounded an answer they had nothing to
do with. The two arms carry their own, differently-shaped floors:

- vector: `embedding <=> :query_vector <= :max_distance`, defaulting to
  `settings.retrieval_max_cosine_distance`. That setting's comment has the
  measurements; the short version is that relevant and irrelevant queries
  land *close together and overlapping* under the default embedder
  (0.556-0.860 against 0.792-1.000 over 33 queries), so this is a useful
  threshold rather than a clean division, and it needs re-measuring on any
  embedder change.
- keyword: the `@@` match itself for the strict (AND) form -- a lexical
  match on *every* content word is already a relevance predicate, which is
  what the vector arm was missing -- plus a bare `ts_rank_cd > 0`, which
  exists only to reject a *bare negation* ("-cat" -> `!'cat'`, matched by
  `@@` against the whole corpus at rank exactly 0.0; see `_KEYWORD_ALL_
  TERMS_SQL`'s comment) and costs nothing against a genuine match, which is
  never scored zero. The OR fallback, which requires only one word of
  several rather than all of them, is a much weaker predicate and needs a
  real, calibrated floor: `ts_rank_cd >= :min_rank`
  (`settings.retrieval_min_keyword_rank`). The two floors are not
  interchangeable -- a single matched lexeme scores `ts_rank_cd` ~0.1, under
  that OR-arm floor, so reusing it on the AND arm would reject genuine
  single-term matches (a product code, a proper noun) that arm exists to
  catch.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rrf import fuse_rrf, top_fused
from app.core.tenancy import TenantContext
from app.embeddings.base import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.rag.vector_sql import vector_literal


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    score: float
    rank: int
    page: int | None


@dataclass(frozen=True, slots=True)
class CitationPayload:
    """One retrieved passage's SSE- and tool-facing shape.

    Deliberately narrower than `RetrievedChunk`: `excerpt` is a short
    preview (see `excerpt()` below), not the full chunk `content`, which
    would double the bytes of every grounded turn on the wire for no
    benefit the UI needs -- it already has `chunk_id` to fetch the rest on
    demand.

    Lives here rather than in `app/chat/service.py` (where it originated)
    because `app/tools/base.py`'s `ToolResult.citations` also needs it: a
    tool result and an SSE turn should report a grounding chunk identically
    rather than through two shapes that drift, and `app/chat/service.py`
    imports `app/tools` (Phase 4's tool layer), so the reverse import would
    cycle.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    rank: int
    score: float
    excerpt: str
    # `None` for a non-paginated source (plain text, Markdown, HTML) --
    # `RetrievedChunk.page` is `None` there too, since `_page_for_offset` in
    # `app/rag/chunk.py` only ever gets a page list from `extract()` for a
    # PDF. Absent is the honest state, not a value to fake as `1`.
    page: int | None


# Public (no leading underscore): `app/chat/service.py` imports this pair
# directly for its own, unrelated excerpting need (`ChatToolCallResult.result`,
# a tool's plain-text result -- never the full payload on the wire, the same
# reason a citation carries an excerpt rather than a whole chunk). Both
# operate on a plain `str`, and there is no reason the bound or the
# truncation shape should ever drift between the two call sites, so this is
# the one definition, not a second copy kept in step by hand -- which is
# exactly what happened here for most of Phase 4 (a byte-identical private
# copy sat in `app/chat/service.py` until review round 2 pointed out that
# reaching across a module boundary for a leading-underscore name was the
# wrong way to share it).
EXCERPT_MAX_CHARS = 240


def excerpt(content: str) -> str:
    if len(content) <= EXCERPT_MAX_CHARS:
        return content
    return content[:EXCERPT_MAX_CHARS].rstrip() + "..."


def build_citation(chunk: RetrievedChunk) -> CitationPayload:
    """The one place a `RetrievedChunk` becomes the SSE- and tool-facing
    `CitationPayload` -- used by `app/tools/retrieve.py`'s
    `RetrieveKnowledgeTool` so a tool result and a chat turn report a
    grounding chunk identically."""
    return CitationPayload(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        rank=chunk.rank,
        score=chunk.score,
        excerpt=excerpt(chunk.content),
        page=chunk.page,
    )


@dataclass(frozen=True, slots=True)
class _CandidateInfo:
    """Everything about a chunk *except* its fused score/rank -- captured
    once, from whichever candidate list first produced this chunk, since
    both lists describe the same row and there is nothing to gain by
    re-reading it twice."""

    document_id: uuid.UUID
    document_title: str
    content: str
    page: int | None


# Both queries select the same columns in the same order so the row-handling
# code below does not need to know which list a row came from. Joined to
# `documents` for the title `RetrievedChunk` reports, with `organization_id`
# bound *twice* -- once on `document_chunks`, once on the join condition
# itself -- rather than once and trusting the `document_id` foreign key to
# carry the same organization on both sides. `DocumentService.replace_chunks`
# already treats that as not a given (see its docstring: a chunk INSERT can
# attach to another org's document_id because the FK check itself does not
# consult RLS); binding organization_id independently on both tables here
# means a chunk that ever ended up mislinked that way still cannot leak
# another org's document title through this join.
# `d.status = 'ready'` closes Phase 3's first carried debt: without it, a
# document the dashboard shows as `failed` can still ground answers.
# Concretely -- a document ingests successfully (status=ready, chunks
# written), a *later* re-ingest (a re-upload, a retry) fails during
# extraction before `replace_chunks` ever runs, so `documents.status` flips
# to `failed` while the *old* chunks -- still attached to that document_id --
# are untouched in `document_chunks`. Nothing upstream of this filter ever
# revisits them, so they keep being retrieved and cited forever, even though
# the UI has already told the user this document failed. Filtering here,
# rather than in `DocumentService` or at ingestion time, is the one place
# that is guaranteed to run on every retrieval regardless of how a document
# got into a bad state.
_VECTOR_SQL = text(
    "SELECT dc.id AS id, dc.document_id AS document_id, d.title AS title, "
    "dc.content AS content, dc.metadata AS metadata "
    "FROM document_chunks dc "
    "JOIN documents d ON d.id = dc.document_id AND d.organization_id = :organization_id "
    "WHERE dc.organization_id = :organization_id "
    "AND d.status = 'ready' "
    "AND dc.embedding <=> CAST(:query_vector AS vector) <= :max_distance "
    "ORDER BY dc.embedding <=> CAST(:query_vector AS vector) ASC "
    "LIMIT :candidates"
)

# `websearch_to_tsquery` ANDs every content word of the input, which is the
# right default and the wrong one for a chat product. A natural question
# always carries a word the corpus does not have, so the whole keyword arm
# returns nothing while the vector arm keeps answering and the feature still
# looks like it works. Measured against a real ingested handbook:
#
#   "How long do I have to file a return?"  -> long & file & return  -> 0 rows
#   "file a return"                         -> file & return         -> 1 row
#   "What does the powertrain warranty cover exactly?"
#       -> powertrain & warranti & cover & exact                     -> 0 rows
#
# That matters more than a missing arm usually would, because this is the
# only arm that stems: `HashingEmbedder` is an unstemmed bag of words, so
# "return" and "returns" are different buckets to the vector side.
#
# The OR form is derived from `websearch_to_tsquery`'s own *parsed output*,
# not from the user's raw text. `websearch_to_tsquery` stays on the
# user-input path -- it is what stops a stray `&` or `:` from becoming a
# syntax error and a 500 -- and its `::text` rendering is a normalized
# tsquery literal with every lexeme single-quoted, so swapping the `&`
# operators for `|` cannot produce anything that fails to parse. (A lexeme
# that itself contains `&`, e.g. a URL token, comes back as a slightly
# different lexeme; still valid, still no injection.)
_AND_TSQUERY = "websearch_to_tsquery('english', :query_text)"
_OR_TSQUERY = "replace(websearch_to_tsquery('english', :query_text)::text, '&', '|')::tsquery"

_KEYWORD_SQL_TEMPLATE = (
    "SELECT dc.id AS id, dc.document_id AS document_id, d.title AS title, "
    "dc.content AS content, dc.metadata AS metadata "
    "FROM document_chunks dc "
    "JOIN documents d ON d.id = dc.document_id AND d.organization_id = :organization_id "
    "WHERE dc.organization_id = :organization_id "
    "AND d.status = 'ready' "
    "AND dc.content_tsv @@ {tsquery} "
    "{rank_floor}"
    "ORDER BY ts_rank_cd(dc.content_tsv, {tsquery}) DESC "
    "LIMIT :candidates"
)

# The AND form used to ship with no floor at all, on the reasoning that
# "every content word is present" already *is* a relevance predicate -- true
# for an ordinary match, but false for the one shape that reaches this arm
# without ever having matched anything: a **bare negation**.
# `websearch_to_tsquery` reads a leading hyphen as negation, so "-cat" parses
# to `!'cat'`, which `@@` matches against every chunk that merely lacks the
# word "cat" -- i.e. the entire corpus -- with `ts_rank_cd` scoring every one
# of those matches exactly 0.0. Without a floor here, that satisfies
# `WHERE ... @@ {tsquery}` and returns up to `candidates` rows from a query
# that named no positive term at all, and the OR fallback below (guarded by
# `if not keyword_rows`) never even runs because the AND form did not return
# zero rows. Phase 3 had already reasoned through the *multi-term* case
# ("-cat dog" -> `!'cat' & 'dog'` -> zero AND rows -> the OR fallback ->
# `!'cat' | 'dog'`, matches everything without "cat", caught by the OR
# form's own floor) but not this one, single-term case, where the AND form
# itself is the whole match and has no floor to catch it.
#
# The fix is `> 0`, not `>= settings.retrieval_min_keyword_rank`. An earlier
# version of this reused that setting here and it was a real regression, not
# a stricter version of the same fix: `retrieval_min_keyword_rank` (0.15) is
# calibrated for the OR arm's own failure mode, where its docstring says the
# floor exists because "at least one content word is present" is a *weak*
# predicate -- a single incidental shared word scores ~0.1 there and must be
# rejected. The AND arm's predicate is the opposite strength: reaching this
# arm at all already means *every* content word matched. A short, exact
# query -- a single product code, a proper noun, the "-cat"-shaped case
# apart -- is exactly the case this arm exists to catch (see the module
# docstring and `docs/ARCHITECTURE.md` §6.2), and `ts_rank_cd` for a single
# matched lexeme is ~0.1: below 0.15, so reusing that constant here silently
# dropped every genuine single-term AND match, which is worse than the bug
# it fixed. A bare negation scores *exactly* 0.0 -- never positive, because
# `ts_rank_cd` cannot reward matching the absence of a word -- so `> 0` is
# the precise boundary between "matched nothing positive" and "matched
# something", with no collateral damage to a real one-word hit.
_KEYWORD_ALL_TERMS_SQL = text(
    _KEYWORD_SQL_TEMPLATE.format(
        tsquery=_AND_TSQUERY,
        rank_floor=f"AND ts_rank_cd(dc.content_tsv, {_AND_TSQUERY}) > 0 ",
    )
)
# The OR form keeps `settings.retrieval_min_keyword_rank` -- this is the
# floor that setting was actually calibrated against (see its docstring).
_KEYWORD_ANY_TERM_SQL = text(
    _KEYWORD_SQL_TEMPLATE.format(
        tsquery=_OR_TSQUERY,
        rank_floor=f"AND ts_rank_cd(dc.content_tsv, {_OR_TSQUERY}) >= :min_rank ",
    )
)


class RetrievalService:
    def __init__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.tenant = tenant
        self.embedder = embedder or get_embedding_provider()

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        candidates: int = 20,
        min_score: float = 0.0,
        max_distance: float | None = None,
        min_keyword_rank: float | None = None,
    ) -> list[RetrievedChunk]:
        """Return up to `top_k` chunks for `query`, fused across vector and
        keyword search and filtered to `score >= min_score`.

        `candidates` bounds how many rows each individual retriever
        contributes before fusion, independent of `top_k` -- fusing over a
        wider candidate pool than the final result size is what lets a
        chunk that is merely decent on both signals outrank one that is
        merely great on one, per RRF's whole premise.

        `max_distance` is the vector arm's relevance floor (cosine
        distance, defaulting to `settings.retrieval_max_cosine_distance`)
        and `min_keyword_rank` is the floor on the keyword arm's fallback
        form (`settings.retrieval_min_keyword_rank`). Both are applied
        *before* fusion, for the reason set out in this module's docstring.
        `min_score` still filters the fused score afterwards, but it answers
        a different question -- "how many lists did this appear in, and how
        high" -- and cannot substitute for either.
        """
        settings = get_settings()
        if max_distance is None:
            max_distance = settings.retrieval_max_cosine_distance
        if min_keyword_rank is None:
            min_keyword_rank = settings.retrieval_min_keyword_rank
        [query_vector] = await self.embedder.embed([query])
        organization_id = self.tenant.organization_id

        vector_rows = (
            await self.session.execute(
                _VECTOR_SQL,
                {
                    "query_vector": vector_literal(query_vector),
                    "candidates": candidates,
                    "organization_id": organization_id,
                    "max_distance": max_distance,
                },
            )
        ).all()
        keyword_params = {
            "query_text": query,
            "candidates": candidates,
            "organization_id": organization_id,
            "min_rank": min_keyword_rank,
        }
        keyword_rows = (await self.session.execute(_KEYWORD_ALL_TERMS_SQL, keyword_params)).all()
        if not keyword_rows:
            # Only on a miss, so an exact-ish query keeps the strict form's
            # precision and pays for one extra statement only when the strict
            # form had nothing to give. RRF supplies the precision back on
            # this path: a chunk matching several query terms ranks above one
            # matching a single term, `ts_rank_cd` orders them that way, and
            # fusion with the vector arm settles the rest.
            keyword_rows = (await self.session.execute(_KEYWORD_ANY_TERM_SQL, keyword_params)).all()

        # `info` is captured from whichever list first produced a chunk --
        # separate from fusion itself (`fuse_rrf` below), which only ever
        # sees ids and rank positions. An empty list here (no lexical match
        # at all, or a corpus with no embeddings) simply contributes
        # nothing to the fused scores -- it can never zero out whatever the
        # other retriever already found.
        info: dict[uuid.UUID, _CandidateInfo] = {}
        for rows in (vector_rows, keyword_rows):
            for row in rows:
                if row.id not in info:
                    metadata = row.metadata or {}
                    info[row.id] = _CandidateInfo(
                        document_id=row.document_id,
                        document_title=row.title,
                        content=row.content,
                        page=metadata.get("page"),
                    )

        scores = fuse_rrf([[row.id for row in vector_rows], [row.id for row in keyword_rows]])
        ordered = top_fused(scores, top_k=top_k, min_score=min_score)

        results: list[RetrievedChunk] = []
        for chunk_id, score in ordered:
            candidate = info[chunk_id]
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document_id=candidate.document_id,
                    document_title=candidate.document_title,
                    content=candidate.content,
                    score=score,
                    rank=len(results) + 1,
                    page=candidate.page,
                )
            )
        return results
