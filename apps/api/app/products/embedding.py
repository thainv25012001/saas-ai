"""Turn product rows into vectors -- Task 4's semantic search arm has
nothing to search until this runs.

`embeddable_text` (app/products/embedding_text.py) is Task 1's, and this
module is its second caller: everything below embeds
`embeddable_text(name, description, attributes)`, never a concatenation
composed here. That is not a style preference -- `Product.embedding_source_hash`
is a hash of that exact function's output, and if this module ever built its
own version of "the text to embed" the hash would certify a vector computed
from different text than the one actually embedded, which is worse than no
hash at all because it *reads* as a guarantee.

Batching, retry and the all-or-nothing contract mirror `app/rag/ingest.py`'s
`_embed_all` deliberately, not coincidentally: both are "embed a list of
texts, batched, with per-batch retry, and never hand back a partial result
a caller could persist half of." A batch that exhausts its retries raises
instead of returning the vectors collected so far -- see `embed_texts`.
"""

import asyncio
from collections.abc import Sequence

from sqlalchemy import ColumnElement, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Product
from app.embeddings.base import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.products.embedding_text import embeddable_text, hash_embeddable_text


async def _embed_batch_with_retry(
    provider: EmbeddingProvider, batch: list[str], max_retries: int, backoff_seconds: float
) -> list[list[float]]:
    attempt = 0
    while True:
        try:
            return await provider.embed(batch)
        except Exception:
            attempt += 1
            if attempt >= max_retries:
                raise
            await asyncio.sleep(backoff_seconds * attempt)


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed `texts`, batched (default 64, `settings.embedding_batch_size`)
    and retried per batch, all-or-nothing.

    Nothing is returned until every batch has succeeded: a batch that
    exhausts its retries raises straight out of this function, with the
    vectors from any earlier, already-succeeded batches simply discarded
    along with the stack frame. There is no partial return value for a
    caller to accidentally persist -- see `embed_and_store` below for the
    write-path consequence.

    Returns `([], provider.name)` for an empty input without ever calling
    the provider -- `range(0, 0, batch_size)` is empty, so the loop below
    does not execute, and calling a provider with an empty batch is not a
    case any of them are obliged to handle sensibly.
    """
    settings = get_settings()
    provider = get_embedding_provider()
    batch_size = settings.embedding_batch_size
    max_retries = settings.embedding_max_retries
    backoff_seconds = settings.embedding_retry_backoff_seconds
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(await _embed_batch_with_retry(provider, batch, max_retries, backoff_seconds))
    return vectors, provider.name


def product_embedding_text(product: Product) -> str:
    """The text `product`'s embedding is (or would be) computed from.

    A thin wrapper, not a reimplementation: it exists so every caller in
    this module reaches `embeddable_text` through one place, rather than
    each writing out `embeddable_text(p.name, p.description, p.attributes)`
    -- see the module docstring for why a second, independent
    concatenation would be a real bug, not just a style issue.
    """
    return embeddable_text(product.name, product.description, product.attributes)


async def embed_products(products: Sequence[Product]) -> tuple[list[list[float]], str]:
    """`embed_texts` over a batch of already-loaded `Product` rows."""
    return await embed_texts([product_embedding_text(product) for product in products])


async def embed_and_store(session: AsyncSession, products: Sequence[Product]) -> None:
    """Compute and persist embeddings for already-persisted `Product` rows
    -- the second phase of a two-phase import (docs/PHASE-5.md §5/§8: rows
    land first, an arq job embeds them separately) and what a future
    reconciliation job re-embeds against (see `needs_reembedding` below for
    how it would pick its candidates).

    Not routed through `ProductService.upsert_many`: these rows already
    exist with every other column populated, so there is nothing to
    upsert -- rebuilding a full `ProductInput` per row just to run it back
    through the `ON CONFLICT` path would be overhead for what is actually a
    plain `UPDATE` of four columns.

    All-or-nothing, transitively from `embed_texts`: no `Product` attribute
    is mutated until every vector in the batch has come back, so a batch
    that exhausts its retries raises with every row's ORM state exactly as
    it was before this was called -- no half-embedded catalogue, and
    nothing for the caller to roll back.
    """
    if not products:
        return
    vectors, embedding_model = await embed_products(products)
    for product, vector in zip(products, vectors, strict=True):
        product.embedding = vector
        product.embedding_model = embedding_model
        # Computed fresh from this same write's content, exactly like
        # `ProductService.upsert_many`'s "embedding supplied" branch: the
        # vector was just computed from `product_embedding_text(product)`,
        # so the hash of that same text is what "this vector is current"
        # means, and staleness (relative to text that hasn't changed since)
        # is false by construction.
        product.embedding_source_hash = hash_embeddable_text(
            product.name, product.description, product.attributes
        )
        product.embedding_stale = False
    await session.flush()


def needs_reembedding(product: Product) -> bool:
    """Does `product` need (re-)embedding? Named because Task 1's review
    found this disambiguation unnamed: a product has three states, and only
    two of them mean "someone should embed this row" --

    - never embedded (`embedding_source_hash IS NULL`): expected mid a
      two-phase import, nothing wrong yet -- needs embedding.
    - embedded and current (hash matches, not stale): nothing to do.
    - embedded from different text (`embedding_stale`): actively describing
      the wrong thing -- needs re-embedding.

    Task 3's import and any future reconciliation job are the callers this
    exists for, so neither has to re-derive "hash is null or stale" from
    the column combination itself every time it asks the question.
    """
    return product.embedding_source_hash is None or product.embedding_stale


def needs_reembedding_clause() -> ColumnElement[bool]:
    """`needs_reembedding`, as a SQL predicate -- for a caller selecting
    candidates directly (`select(Product).where(needs_reembedding_clause())`)
    rather than loading a table to filter it in Python. Kept in lockstep
    with `needs_reembedding` by definition, not merely by convention: both
    exist to answer the exact same question, one over a row already in
    hand and one over rows still in the database.
    """
    return or_(Product.embedding_source_hash.is_(None), Product.embedding_stale.is_(True))
