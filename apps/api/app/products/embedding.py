"""Turn product rows into vectors -- Task 4's semantic search arm has
nothing to search until this runs.

`embeddable_text` (app/products/embedding_text.py) is Task 1's, and this
module is its second caller: everything below embeds
`embeddable_text(name, description, attributes)`, never a concatenation
composed here. That is not a style preference -- `Product.embedding_source_hash`
is a hash of that exact function's output, and if this module ever built its
own version of "the text to embed" the hash would certify a vector computed
from different text than the one actually embedded, which is worse than no
hash at all because it *reads* as a guarantee. `embed_and_store` goes
further than merely calling the right function: it hashes the exact string
it embedded (`hash_text`), not a re-derivation from the row's current
attributes, so the two cannot disagree even across the `await` in between --
see its docstring.

Batching and retry are `app.embeddings.batch.embed_batched`, shared with
`app/rag/ingest.py`'s `_embed_all` -- see that module's docstring for why
this used to be two copies of the same loop and now is not.
"""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import TenantContext
from app.db.models import Product
from app.embeddings.batch import embed_batched
from app.embeddings.registry import get_embedding_provider
from app.products.embedding_text import embeddable_text, hash_text


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed `texts`, batched (default 64, `settings.embedding_batch_size`)
    and retried per batch, all-or-nothing -- `embed_batched` is the actual
    loop; this resolves settings/provider and attaches `provider.name`.

    Nothing is returned until every batch has succeeded: a batch that
    exhausts its retries raises straight out of this function, with the
    vectors from any earlier, already-succeeded batches simply discarded
    along with the stack frame. There is no partial return value for a
    caller to accidentally persist -- see `embed_and_store` below for the
    write-path consequence.

    Returns `([], provider.name)` for an empty input without ever calling
    the provider -- see `embed_batched`.
    """
    settings = get_settings()
    provider = get_embedding_provider()
    vectors = await embed_batched(
        texts,
        provider,
        settings.embedding_batch_size,
        settings.embedding_max_retries,
        settings.embedding_retry_backoff_seconds,
    )
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


async def embed_and_store(
    session: AsyncSession, tenant: TenantContext, products: Sequence[Product]
) -> None:
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

    **`tenant` is required and checked against every product before
    anything is embedded or written** (docs/ARCHITECTURE.md §2.3's Layer 1
    -- an explicit predicate, not just RLS). This mutates already-loaded
    ORM objects by primary key rather than issuing a query with its own
    `WHERE organization_id = ...`, so unlike every `ProductService` method
    it had no Layer 1 of its own; RLS (Layer 2) still blocks a cross-tenant
    write underneath it, but RLS is a database policy, not the tenancy
    boundary itself, and nothing here should depend on a mechanism that
    exists for a different purpose (Postgres's row security) continuing to
    cover a case it was never written for. Checked up front, before the
    loop, so a caller that hands in a mixed-tenant list gets a clean
    refusal instead of a partially-applied embed.

    The exact text embedded for each product is captured once, into
    `texts`, before the (`await`-ing) call to `embed_texts` -- and that
    same captured string, not a fresh read of `product.name`/`description`/
    `attributes` afterward, is what gets hashed into
    `embedding_source_hash` below. Re-reading the row's current attributes
    after the `await` would let the hash and the vector describe different
    text if anything mutated the row in between; hashing the string that
    was actually sent to the provider makes that structurally impossible
    rather than merely unlikely.

    All-or-nothing, transitively from `embed_texts`: no `Product` attribute
    is mutated until every vector in the batch has come back, so a batch
    that exhausts its retries raises with every row's ORM state exactly as
    it was before this was called -- no half-embedded catalogue, and
    nothing for the caller to roll back.
    """
    if not products:
        return
    mismatched = [p.external_id for p in products if p.organization_id != tenant.organization_id]
    if mismatched:
        raise ValueError(
            f"embed_and_store called for organization_id={tenant.organization_id} but "
            f"these products belong to a different organization: {mismatched}"
        )
    texts = [product_embedding_text(product) for product in products]
    vectors, embedding_model = await embed_texts(texts)
    for product, vector, text in zip(products, vectors, texts, strict=True):
        product.embedding = vector
        product.embedding_model = embedding_model
        # Hashes the captured `text` this vector was actually computed
        # from, not a fresh `product.name`/`description`/`attributes` read
        # -- see the docstring above.
        product.embedding_source_hash = hash_text(text)
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
