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
  ordered by `ts_rank_cd` descending. `websearch_to_tsquery` specifically,
  not `to_tsquery`: the latter raises on bare `&`/`|`/`:`/unbalanced quotes
  in the input, which for a query built from a customer's own words is a
  matter of when, not if.

The two lists are combined with Reciprocal Rank Fusion rather than trying
to make cosine distance and `ts_rank_cd` commensurable -- they are on
unrelated scales and neither is a probability, so there is no principled
way to add them directly. RRF sidesteps that by fusing on rank position
alone.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.embeddings.base import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider

# Reciprocal Rank Fusion's published constant (Cormack, Clarke & Buettcher,
# 2009, the paper that introduced RRF): score = sum(1 / (k + rank)) over the
# lists a document appears in. k=60 is the value that paper found robust
# across corpora and is the de facto default everywhere RRF is used --
# treated here as a fixed convention, not something to tune per query.
_RRF_K = 60


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
_VECTOR_SQL = text(
    "SELECT dc.id AS id, dc.document_id AS document_id, d.title AS title, "
    "dc.content AS content, dc.metadata AS metadata "
    "FROM document_chunks dc "
    "JOIN documents d ON d.id = dc.document_id AND d.organization_id = :organization_id "
    "WHERE dc.organization_id = :organization_id "
    "ORDER BY dc.embedding <=> CAST(:query_vector AS vector) ASC "
    "LIMIT :candidates"
)

_KEYWORD_SQL = text(
    "SELECT dc.id AS id, dc.document_id AS document_id, d.title AS title, "
    "dc.content AS content, dc.metadata AS metadata "
    "FROM document_chunks dc "
    "JOIN documents d ON d.id = dc.document_id AND d.organization_id = :organization_id "
    "WHERE dc.organization_id = :organization_id "
    "AND dc.content_tsv @@ websearch_to_tsquery('english', :query_text) "
    "ORDER BY ts_rank_cd(dc.content_tsv, websearch_to_tsquery('english', :query_text)) DESC "
    "LIMIT :candidates"
)


def _vector_literal(values: list[float]) -> str:
    """Render an embedding as a pgvector text-input literal, e.g. "[0.1,-0.2]".

    Passed through `CAST(:param AS vector)` rather than a driver-level
    pgvector codec (none is registered on this session's asyncpg
    connections) or `::vector` cast syntax (which does not parse through
    SQLAlchemy's `:param` binding at all) -- the same pattern the ingestion
    tests already use for seeding vector columns via raw SQL.
    """
    return "[" + ",".join(repr(value) for value in values) + "]"


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
    ) -> list[RetrievedChunk]:
        """Return up to `top_k` chunks for `query`, fused across vector and
        keyword search and filtered to `score >= min_score`.

        `candidates` bounds how many rows each individual retriever
        contributes before fusion, independent of `top_k` -- fusing over a
        wider candidate pool than the final result size is what lets a
        chunk that is merely decent on both signals outrank one that is
        merely great on one, per RRF's whole premise.
        """
        [query_vector] = await self.embedder.embed([query])
        organization_id = self.tenant.organization_id

        vector_rows = (
            await self.session.execute(
                _VECTOR_SQL,
                {
                    "query_vector": _vector_literal(query_vector),
                    "candidates": candidates,
                    "organization_id": organization_id,
                },
            )
        ).all()
        keyword_rows = (
            await self.session.execute(
                _KEYWORD_SQL,
                {
                    "query_text": query,
                    "candidates": candidates,
                    "organization_id": organization_id,
                },
            )
        ).all()

        scores: dict[uuid.UUID, float] = {}
        info: dict[uuid.UUID, _CandidateInfo] = {}
        # An empty list here (no lexical match at all, or a corpus with no
        # embeddings) simply contributes nothing to `scores` -- it can never
        # zero out whatever the other retriever already found, because
        # fusion only ever adds to a chunk's score, never resets it.
        for rows in (vector_rows, keyword_rows):
            for rank, row in enumerate(rows, start=1):
                chunk_id = row.id
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
                if chunk_id not in info:
                    metadata = row.metadata or {}
                    info[chunk_id] = _CandidateInfo(
                        document_id=row.document_id,
                        document_title=row.title,
                        content=row.content,
                        page=metadata.get("page"),
                    )

        # Ties (identical fused score) break on chunk_id purely for a
        # deterministic order across runs -- no ranking significance.
        ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))

        results: list[RetrievedChunk] = []
        for chunk_id, score in ordered:
            if score < min_score:
                # `ordered` is sorted descending by score, so nothing after
                # this point can clear the threshold either.
                break
            if len(results) >= top_k:
                break
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
