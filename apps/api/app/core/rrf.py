"""Reciprocal Rank Fusion -- the algorithm `app/rag/retrieve.py` (document
chunks) and `app/rag/products.py` (products) both need to combine
independent ranked candidate lists (vector distance, keyword rank, ...)
into one order, without trying to make their scores commensurable. See
`app/rag/retrieve.py`'s module docstring for why that would not be
principled in the first place: cosine distance and `ts_rank_cd` are on
unrelated scales and neither is a probability, so there is no way to add
them directly that means anything.

Extracted here rather than left as two copies (Phase 5 Task 4, Ruling 3:
"if this task needs the fusion, it extracts it to a shared home and
converts `retrieve.py` to the shared version in the same commit -- never a
copy"). Task 2 was held to the identical standard for the embedding batch
-and-retry loop, now `app/embeddings/batch.py`, and a later review
confirmed a bug fixed in that shared loop surfaces to both consumers --
the same property this module exists to have for RRF.

Lives in `app/core`, not `app/rag`: the algorithm knows nothing about
chunks, products, or any particular domain row, only about opaque ids and
their rank position in each list -- the same reasoning that put
`embed_batched` beside the embedding-provider abstraction instead of in
either of its callers' domains.
"""

import uuid
from collections.abc import Sequence

# Reciprocal Rank Fusion's published constant (Cormack, Clarke & Buettcher,
# 2009, the paper that introduced RRF): score = sum(1 / (k + rank)) over the
# lists a document appears in. k=60 is the value that paper found robust
# across corpora and is the de facto default everywhere RRF is used --
# every caller in this codebase shares it as a fixed convention rather than
# each picking its own, so "what does k mean here" has one answer.
RRF_K = 60


def fuse_rrf(
    rank_lists: Sequence[Sequence[uuid.UUID]],
    *,
    k: int = RRF_K,
) -> dict[uuid.UUID, float]:
    """Fuse any number of independently-ranked id lists into one score per
    id: `sum(1 / (k + rank))` over every list an id appears in, with ranks
    counted 1-based within each list.

    An id absent from a list simply contributes nothing from that list --
    fusion only ever adds to a score, never resets it, so an id found by
    every list outranks one found by only its single best list. That
    (summing, not taking each id's best rank) is the property worth
    pinning with a test: see `test_chunk_found_by_both_retrievers_outranks
    _the_single_best_vector_match` in `tests/integration/test_retrieve.py`
    and its product-search counterpart in `tests/integration/
    test_product_search.py` for why a max-of-best-rank shortcut would still
    pass a weaker version of the same test.
    """
    scores: dict[uuid.UUID, float] = {}
    for ids in rank_lists:
        for rank, id_ in enumerate(ids, start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank)
    return scores


def top_fused(
    scores: dict[uuid.UUID, float],
    *,
    top_k: int,
    min_score: float = 0.0,
) -> list[tuple[uuid.UUID, float]]:
    """`scores` (from `fuse_rrf`), ordered descending and cut to the first
    `top_k` ids scoring at least `min_score`.

    Ties (identical fused score) break on the id's own string form, purely
    for a deterministic order across runs -- no ranking significance.

    A fused RRF score carries no information about *how good* a match is:
    the top hit is `1/(k+1)` whether it answers the question or is the
    least-bad candidate in an otherwise-irrelevant corpus. So `min_score`
    here can only ever mean "appeared in one list" versus "appeared in
    several", never "is relevant" -- a real relevance floor belongs on each
    arm's own query, before fusion (see `app/rag/retrieve.py`'s module
    docstring for the fuller argument and the floors it applies there).
    """
    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    result: list[tuple[uuid.UUID, float]] = []
    for id_, score in ordered:
        if score < min_score:
            # `ordered` is sorted descending, so nothing after this point
            # can clear the threshold either.
            break
        if len(result) >= top_k:
            break
        result.append((id_, score))
    return result
