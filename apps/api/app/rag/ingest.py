"""The ingestion pipeline: bytes -> extracted text -> chunks -> embeddings -> stored rows.

`ingest_document` takes an already-open session rather than opening its own,
so the same function drives both a direct test (against a `tenant_session`
fixture already holding a transaction) and the arq worker's own
`tenant_session` (`app.workers.tasks.ingest_document_task`) identically.

Commit discipline is the crux of this module, not a detail, and it is split
three ways across three separate transactions -- not the two it might look
like at first:

- `status=processing` is committed through its own independent
  `tenant_session`, before this function touches `session` (the caller's
  own transaction) at all. This is what makes `processing` an actually
  observable state rather than one that only ever exists inside a
  transaction nobody outside this call can see until it finally commits or
  rolls back -- Task 8's Knowledge page polls this column for a status
  badge while a large document is mid-pipeline.
- On success, the new chunks and `status=ready` are committed together on
  `session` -- but by the *caller's* own `tenant_session`, not by this
  function. `ingest_document` never calls `session.commit()` itself on the
  success path: whatever opened `session` is the only thing that knows
  whether committing it now is safe (a caller that does more work on
  `session` afterward needs that transaction to still be open).
- On any failure, `status=failed` (with the error) is committed
  unconditionally through a *third*, independent transaction, then the
  original exception is re-raised. It cannot simply commit `session` the
  way a caller's own success commit does: the failure may itself be a
  database error (e.g. a constraint violation inside `replace_chunks`),
  which leaves `session` unable to do anything further while still nested
  inside the caller's own transaction (see the comment in the `except`
  block below for why). Writing the failure through its own transaction
  sidesteps that entirely and guarantees the commit happens regardless --
  the same commit-vs-rollback distinction Phase 2's SSE endpoint got wrong
  once, leaving a row silently unwritten. Without it, a failed ingest would
  roll all the way back to the row's previous status and the document
  would sit in `processing` forever with nothing to retry it and no record
  of why.

Embedding failure is deliberately all-or-nothing: `_embed_all` only returns
once every chunk in the document has a vector, and any batch that exhausts
its retries raises instead of returning a partial list. That is what keeps
`replace_chunks` from ever being called with a subset of the corpus -- a
half-embedded document that reports `ready` would silently return incomplete
answers forever, which is worse than failing loudly.
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import TenantContext, tenant_session
from app.documents.schemas import ChunkInput
from app.documents.service import DocumentService
from app.embeddings.base import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.rag.chunk import chunk_document
from app.rag.extract import extract

# Truncated so a pathological exception message (e.g. an HTML error page a
# provider returned as its body) cannot dominate the `error` column or bury
# the useful part of the message under noise nobody will read.
_ERROR_MESSAGE_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class IngestResult:
    chunk_count: int
    token_count: int
    embedding_model: str


def _truncate(message: str) -> str:
    if len(message) <= _ERROR_MESSAGE_LIMIT:
        return message
    return message[:_ERROR_MESSAGE_LIMIT] + "... (truncated)"


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


async def _embed_all(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed every chunk, batched and retried, all-or-nothing.

    Returns `([], provider.name)` for a document with no chunks (an empty
    input) without ever calling the provider -- `range(0, 0, batch_size)`
    is empty, so the loop below simply does not execute; there is nothing
    to embed, and calling a provider with an empty batch is not a case any
    of them are obliged to handle sensibly.
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


async def ingest_document(
    session: AsyncSession,
    tenant: TenantContext,
    document_id: uuid.UUID,
    data: bytes,
    mime_type: str,
) -> IngestResult:
    """Run the whole pipeline for one document, inside the caller's session.

    Sets `status=processing` on entry, committed through its own
    transaction; on success, replaces the document's chunks and sets
    `status=ready` + `processed_at` on `session`, left for the *caller* to
    commit; on any exception, sets `status=failed` with a truncated message
    in `error` through yet another independent transaction, and re-raises.
    See the module docstring for why each of those three writes needs its
    own transaction rather than sharing one.
    """
    async with tenant_session(tenant) as processing_session:
        await DocumentService(processing_session, tenant).mark_processing(document_id)

    service = DocumentService(session, tenant)
    try:
        extracted = extract(data, mime_type)
        chunks = chunk_document(extracted)
        embeddings, embedding_model = await _embed_all([chunk.content for chunk in chunks])
        chunk_inputs = [
            ChunkInput(
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=embedding,
                embedding_model=embedding_model,
                metadata=chunk.metadata,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        await service.replace_chunks(document_id, chunk_inputs)
        await service.mark_ready(document_id)
    except Exception as exc:
        message = _truncate(str(exc))
        # A failure raised from inside the try block above is not always a
        # plain Python exception (extraction/chunking/embedding errors) --
        # it can also be a database error, e.g. a constraint violation
        # inside `replace_chunks`. That kind leaves `session`'s current
        # transaction aborted at the database, and once a transaction ends
        # -- however it ends -- SQLAlchemy will not let *this same session*
        # do anything else while code is still nested inside the
        # `session.begin()` block the caller (`tenant_session`) opened: it
        # raises `InvalidRequestError` on the very next statement, including
        # a fresh `session.begin()`. There is no in-place recovery
        # available here.
        #
        # So this does not try to keep using `session` at all. It records
        # the failure through a second, completely independent
        # `tenant_session` -- its own connection, its own transaction, its
        # own `app.current_org_id` -- which commits on its own the moment
        # this block exits cleanly, regardless of what state `session` is
        # left in. That independence is what makes the commit here
        # unconditional rather than contingent on the failing transaction
        # cooperating -- the same commit-not-rollback distinction Phase 2's
        # SSE endpoint got wrong once.
        #
        # `session` is rolled back first rather than left for the caller's
        # own `tenant_session` to clean up once this exception reaches it.
        # `mark_processing` no longer writes through `session` (it commits
        # through its own independent transaction above, before the `try`
        # even starts), so as this pipeline stands today `session` reaching
        # this point never holds a lock that conflicts with
        # `failure_session`'s own `UPDATE` -- removing this rollback does
        # not currently reproduce the deadlock that first motivated it
        # (verified by removing it and re-running the full suite, including
        # the genuine-database-error test below, with no hang). It stays
        # anyway: it is a correct, cheap thing to do with a session this
        # function is about to stop using regardless, and it is what
        # prevents a *future* change that adds another `documents`-row
        # write to `session` before this point from silently reintroducing
        # exactly that deadlock.
        await session.rollback()
        async with tenant_session(tenant) as failure_session:
            await DocumentService(failure_session, tenant).mark_failed(document_id, message)
        raise

    return IngestResult(
        chunk_count=len(chunk_inputs),
        token_count=sum(chunk.token_count for chunk in chunks),
        embedding_model=embedding_model,
    )
