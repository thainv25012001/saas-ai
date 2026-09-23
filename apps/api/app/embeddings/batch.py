"""Batched, retried embedding -- the loop `app/rag/ingest.py` and
`app/products/embedding.py` both need, and nothing else about either
caller: this is provider-level batching, with no knowledge of RAG chunks
or product rows, so it belongs beside the provider abstraction rather
than duplicated into every module that needs it.

Phase 5's Ruling 3 (docs/PHASE-5.md, RRF fusion) names the exact failure
this extraction closes: a second implementation is never left for a
later cleanup to find, even when a docstring admits the duplication is
deliberate. Before this module existed, `app/rag/ingest.py::_embed_all`
and `app/products/embedding.py::embed_texts` were the same function --
same locals, same batch slicing, same retry/backoff, same all-or-nothing
contract -- in two files. Now there is exactly one.

Deliberately takes `provider`/`batch_size`/`max_retries`/`backoff_seconds`
as plain arguments rather than resolving `get_settings()`/
`get_embedding_provider()` itself: both call sites' existing tests
monkeypatch those two functions on the *caller's* module (`ingest_module`
or `embedding_module`), and this keeps that working unchanged -- a
caller resolves its own settings and provider, then hands the concrete
values in.
"""

import asyncio

from app.embeddings.base import EmbeddingProvider


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


async def embed_batched(
    texts: list[str],
    provider: EmbeddingProvider,
    batch_size: int,
    max_retries: int,
    backoff_seconds: float,
) -> list[list[float]]:
    """Embed `texts` through `provider`, split into batches of `batch_size`
    with per-batch retry, all-or-nothing.

    Nothing is returned until every batch has succeeded: a batch that
    exhausts its retries raises straight out of this function, with the
    vectors from any earlier, already-succeeded batches simply discarded
    along with the stack frame. There is no partial return value for a
    caller to accidentally persist.

    Returns `[]` for an empty `texts` without ever calling `provider` --
    `range(0, 0, batch_size)` is empty, so the loop below does not
    execute, and calling a provider with an empty batch is not a case any
    of them are obliged to handle sensibly.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(await _embed_batch_with_retry(provider, batch, max_retries, backoff_seconds))
    return vectors
