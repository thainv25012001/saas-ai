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

Two ingest jobs for the same document are serialised, not tolerated: a
`pg_advisory_xact_lock` keyed on the document id is taken on `session`
before anything else runs (see the comment at the top of
`ingest_document`). Without it, delete-then-insert plus READ COMMITTED
leaves the loser colliding on `uq_chunk_document_index` and its failure
transaction overwriting the winner's `ready` with `failed`.

Cancellation is handled separately from ordinary failure, because
`asyncio.CancelledError` is a `BaseException` and an `except Exception`
clause never sees it -- see the `except asyncio.CancelledError` block for
what a missed cancellation costs (a document wedged in `processing`
forever, which `retry_document` refuses to touch).

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

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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

# Serialises two ingest jobs for the *same* document against each other,
# and nothing else. `pg_advisory_xact_lock` is held until the transaction
# it was taken in ends, so this is `session`'s transaction -- exactly the
# one `replace_chunks` and `mark_ready` write in -- and it is released by
# whatever the caller does with `session` (commit, rollback), with no
# unlock call to forget.
#
# `hashtext` narrows the document's uuid to the int4 the lock key space
# offers. A collision between two *different* documents is possible and
# costs nothing but a needless wait; a missed collision between two jobs
# for the *same* document is not possible, which is the direction that
# matters.
_ADVISORY_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:document_key))")


@dataclass(frozen=True, slots=True)
class IngestResult:
    chunk_count: int
    token_count: int
    embedding_model: str


def _truncate(message: str) -> str:
    if len(message) <= _ERROR_MESSAGE_LIMIT:
        return message
    return message[:_ERROR_MESSAGE_LIMIT] + "... (truncated)"


def _error_message(exc: Exception) -> str:
    """A bounded, deliberate description of `exc` for `documents.error`.

    Not `str(exc)`. That column is rendered verbatim in the Knowledge
    page's alert, and `str()` on a SQLAlchemy `DBAPIError` is the failing
    statement **and its bound parameters** -- which, for a failure inside
    `replace_chunks`, means the customer's own chunk text and a 1536-float
    embedding. Measured at 1187 characters on the duplicate-key path, with
    the actual cause pushed to the end and, for a longer chunk, truncated
    away entirely by `_ERROR_MESSAGE_LIMIT`: the dashboard showed a wall of
    vector floats instead of the "why" docs/PHASE-3.md §3 promised.

    `exc.orig` is the driver's own exception (`asyncpg.exceptions.*`), whose
    message is the database's -- `duplicate key value violates unique
    constraint "uq_chunk_document_index"` -- with no statement and no
    parameters attached. Prefixing the SQLAlchemy exception's class name
    keeps the category ("IntegrityError", "OperationalError") that tells an
    operator whether this is their problem or ours.

    Non-DBAPI exceptions (extraction, chunking, an embedding provider's
    HTTP error) carry no bound parameters, so their own message is exactly
    what should be shown -- still with the type name in front of it, since
    several of them are raised with short messages that mean nothing
    without it.
    """
    if isinstance(exc, DBAPIError):
        return _truncate(f"{type(exc).__name__}: {exc.orig}")
    return _truncate(f"{type(exc).__name__}: {exc}")


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


async def _record_failure(
    session: AsyncSession,
    tenant: TenantContext,
    document_id: uuid.UUID,
    message: str,
) -> None:
    """Record `status=failed` for a document whose ingest has just raised.

    A failure raised from inside `ingest_document`'s try block is not always
    a plain Python exception (extraction/chunking/embedding errors) -- it can
    also be a database error, e.g. a constraint violation inside
    `replace_chunks`. That kind leaves `session`'s current transaction
    aborted at the database, and once a transaction ends -- however it ends
    -- SQLAlchemy will not let *this same session* do anything else while
    code is still nested inside the `session.begin()` block the caller
    (`tenant_session`) opened: it raises `InvalidRequestError` on the very
    next statement, including a fresh `session.begin()`. There is no in-place
    recovery available there.

    So this does not try to keep using `session` at all. It records the
    failure through a second, completely independent `tenant_session` -- its
    own connection, its own transaction, its own `app.current_org_id` --
    which commits on its own the moment this block exits cleanly, regardless
    of what state `session` is left in. That independence is what makes the
    commit here unconditional rather than contingent on the failing
    transaction cooperating -- the same commit-not-rollback distinction Phase
    2's SSE endpoint got wrong once.

    `session` is rolled back first rather than left for the caller's own
    `tenant_session` to clean up once the exception reaches it. This guards a
    real, reachable window, not a hypothetical one: `mark_ready` is the last
    statement in the caller's try block, and it is itself a write to *this
    same* `documents` row on `session` -- a `FOR NO KEY UPDATE` row lock. A
    failure raised at or after that `UPDATE` (a constraint violation, a
    trigger, a serialization failure -- anything that reaches the handler
    without `session` having committed or rolled back first) leaves `session`
    holding that lock open, and `failure_session`'s own `UPDATE` on the same
    row -- also `FOR NO KEY UPDATE`, which conflicts with itself -- would
    then block on it indefinitely: this function waiting on its own earlier,
    still-open write. `mark_processing` moving to its own independent
    transaction closed the *other* way this used to deadlock, but not this
    one. The rollback also releases the per-document advisory lock
    `ingest_document` took on `session`, which is what lets a second,
    serialised job proceed after a failed first one.

    No test in this suite currently forces that specific window: the
    genuine-database-error test fails earlier, inside `replace_chunks`,
    before `mark_ready` ever runs. The rollback stays unverified by the suite
    for that path, but the path itself is real.
    """
    await session.rollback()
    async with tenant_session(tenant) as failure_session:
        await DocumentService(failure_session, tenant).mark_failed(document_id, message)


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

    **The document row must already be committed** before this is called --
    it cannot be created in the same transaction `session` belongs to.
    `mark_processing` looks it up through its own independent
    `tenant_session` (above), which cannot see a row only flushed, not
    committed, in a different session/connection; and because that lookup
    runs *before* the `try` block, the `NotFoundError` it raises for an
    uncommitted document escapes uncaught, with no `status=failed` ever
    recorded. The worker (`app.workers.tasks.ingest_document_task`)
    satisfies this by construction: it reads the document under its own
    fresh `tenant_session`, necessarily after whatever request created and
    committed it has already returned. Any future caller -- Task 5's
    upload endpoint included -- must commit the document's own creation
    before calling this, not pass it straight through from a still-open
    create.
    """
    # Taken before anything else, including `mark_processing`: a second job
    # for this same document blocks here, on its own connection, until the
    # first one's `session` transaction ends -- rather than racing it into
    # `replace_chunks`. That race is not merely wasteful, it is corrupting:
    # `replace_chunks` is delete-then-insert, so under READ COMMITTED the
    # loser's DELETE cannot see rows the winner inserted after the loser's
    # statement snapshot was taken, it deletes nothing, collides on
    # `uq_chunk_document_index`, and its own failure transaction then
    # overwrites the winner's committed `ready` with `failed`. Everything
    # downstream -- the Knowledge page's badge, the retry gate, and
    # `ChatService._retrieve_context`'s `status=READY` readiness check --
    # reads that field, so a single-document organization silently loses
    # grounding while its corpus sits there intact. Serialising is cheaper
    # than tolerating: the second job simply re-ingests over the first,
    # which `replace_chunks` already does correctly.
    #
    # Residual, deliberately accepted: the failure path below rolls
    # `session` back (which releases this lock) before recording `failed`
    # through its own transaction. A waiting second job is unblocked at
    # that instant, but it cannot finish an entire extract/embed/insert
    # cycle inside the single UPDATE that follows, so it cannot have its
    # `ready` overwritten the way the unserialised race did.
    await session.execute(_ADVISORY_LOCK_SQL, {"document_key": str(document_id)})

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
    except asyncio.CancelledError:
        # `asyncio.CancelledError` derives from `BaseException`, not
        # `Exception`, so without this clause the `except Exception` below
        # never sees it and no `failed` status is ever recorded. That is not
        # a theoretical gap: arq enforces `WorkerSettings.job_timeout` (600s)
        # by cancelling the running task, and the case the spec itself names
        # -- a 200-page PDF whose extraction and embedding legitimately
        # outrun that -- lands here every time. The row was already committed
        # as `processing` (above, in its own transaction, deliberately
        # durable), arq abandons the job after `max_tries`, and
        # `retry_document` rejects `processing` by design: the document is
        # wedged with no way back short of delete-and-re-upload.
        #
        # The cancellation is re-raised, never swallowed. Reporting a
        # cancelled job as successful would break arq's own timeout and
        # shutdown handling and every `wait_for` above this one; the only
        # thing this clause adds is a recorded reason before the cancellation
        # continues on its way. The awaits inside `_record_failure` are safe
        # here because a cancellation is delivered once, at the await it
        # interrupts -- this handler's own awaits are not re-cancelled unless
        # something cancels the task a second time, in which case the row
        # simply stays `processing` exactly as it did before.
        await _record_failure(
            session,
            tenant,
            document_id,
            "ingestion was cancelled before it finished -- most likely the worker's "
            "job timeout or a worker shutdown. Retrying is safe.",
        )
        raise
    except Exception as exc:
        await _record_failure(session, tenant, document_id, _error_message(exc))
        raise

    return IngestResult(
        chunk_count=len(chunk_inputs),
        token_count=sum(chunk.token_count for chunk in chunks),
        embedding_model=embedding_model,
    )
