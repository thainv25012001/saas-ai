"""The ingestion pipeline, end to end against a real (local) Postgres.

Every test here uses `HashingEmbedder` (`settings.embedding_provider`
defaults to `"hashing"`) -- no network call, no API key, and its cosine
similarity is real enough that "every chunk got a 1536-dim vector" is a
meaningful assertion rather than a shape check on mock data.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.db.models import DocumentChunk, DocumentSourceType, DocumentStatus
from app.documents.schemas import CreateDocumentInput
from app.documents.service import DocumentService
from app.rag import ingest as ingest_module
from app.rag import storage as storage_module
from app.rag.extract import UnsupportedDocumentType
from app.rag.ingest import ingest_document
from app.workers.tasks import ingest_document_task

pytestmark = pytest.mark.anyio

_MARKDOWN = (
    b"# Refund Policy\n\n"
    b"Customers may request a refund within 30 days of purchase. Contact "
    b"support with your order number to begin.\n\n"
    b"## Exceptions\n\n"
    b"Digital goods are non-refundable once downloaded.\n"
)


async def _document(session, tenant, **overrides):
    fields: dict[str, object] = {
        "title": "Refund Policy",
        "source_type": DocumentSourceType.TEXT,
        **overrides,
    }
    return await DocumentService(session, tenant).create(CreateDocumentInput(**fields))


async def _chunk_count(owner_connection, document_id: uuid.UUID) -> int:
    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM document_chunks WHERE document_id = :id"),
        {"id": document_id},
    )
    return int(result.scalar_one())


def _vector_literal() -> str:
    """A pgvector text-input literal (`'[0,0,...]'::vector`), for seeding a
    row through raw SQL where no ORM/pgvector codec is registered on the
    connection (`owner_connection` is a plain `AsyncConnection`)."""
    return "[" + ",".join("0" for _ in range(1536)) + "]"


async def test_happy_path_ingests_to_ready_with_embedded_chunks(tenant_a):
    # Created and committed in its own transaction, ahead of the one
    # `ingest_document` runs in -- it looks the document up through its own
    # independent transaction for `mark_processing`, which cannot see a row
    # only flushed (not yet committed) in another session. This also
    # mirrors how the worker actually sees documents: created by an
    # earlier, already-committed upload request.
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        result = await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    assert result.chunk_count > 0
    assert result.embedding_model == "hashing"

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)
        rows = (
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document.id)
                    .order_by(DocumentChunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )

    assert fresh.status == DocumentStatus.READY
    assert fresh.processed_at is not None
    assert len(rows) == result.chunk_count
    for row in rows:
        assert len(row.embedding) == 1536
        assert row.embedding_model == "hashing"


async def test_mark_processing_is_committed_independently_and_observable_mid_ingest(
    tenant_a, monkeypatch
):
    """Probes the document's status from a *different* session while
    `ingest_document` is still in flight -- the only way to prove
    `mark_processing` is actually committed on its own, rather than sitting
    unflushed inside `session`'s transaction until the whole pipeline
    finishes (in which case this would read whatever the row's status was
    *before* this call, then jump straight to `ready`). Task 8's Knowledge
    page polls this column for a status badge while a large document is
    mid-pipeline; a `processing` that is never actually observable would
    make that badge lie for the entire ingest.

    The embedding call is held open on an `asyncio.Event` so there is a
    window, after `mark_processing` has definitely already committed (it
    runs before extraction/chunking/embedding even start) and before
    `mark_ready` runs, in which to read the row from a separate session.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    embedding_started = asyncio.Event()
    release_embedding = asyncio.Event()

    class _BlocksUntilReleased:
        name = "blocks-for-test"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            embedding_started.set()
            await release_embedding.wait()
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(ingest_module, "get_embedding_provider", lambda: _BlocksUntilReleased())

    async def _run_ingest() -> None:
        async with tenant_session(tenant_a) as session:
            await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    task = asyncio.create_task(_run_ingest())
    try:
        await asyncio.wait_for(embedding_started.wait(), timeout=5)

        async with tenant_session(tenant_a) as probe_session:
            mid_flight = await DocumentService(probe_session, tenant_a).get(document.id)
        assert mid_flight.status == DocumentStatus.PROCESSING
    finally:
        release_embedding.set()
        await asyncio.wait_for(task, timeout=5)


async def test_extraction_failure_marks_failed_and_the_commit_survives_in_a_fresh_session(
    tenant_a,
):
    """The exception must propagate past the *outer* `tenant_session` too --
    catching it earlier would let that block's own commit-on-clean-exit
    paper over a missing internal commit, and the test would pass either
    way. Letting it propagate means the block's `session.begin()` rolls
    back on exit, and only `ingest_document` recording the failure through
    its own independent transaction, before it re-raises, can make the
    failed status survive that rollback.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    with pytest.raises(UnsupportedDocumentType):
        async with tenant_session(tenant_a) as session:
            await ingest_document(
                session, tenant_a, document.id, b"whatever", "application/x-bogus"
            )

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.FAILED
    assert fresh.error is not None and "x-bogus" in fresh.error


async def test_reingesting_the_same_document_replaces_rather_than_appends(
    tenant_a, owner_connection
):
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        first = await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    async with tenant_session(tenant_a) as session:
        second = await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    assert first.chunk_count == second.chunk_count
    assert await _chunk_count(owner_connection, document.id) == second.chunk_count


async def test_embedding_failure_after_retries_fails_document_and_leaves_zero_chunks(
    tenant_a, owner_connection, monkeypatch
):
    """`_MARKDOWN` produces two chunks (asserted by the happy-path test's
    `chunk_count`); forcing `embedding_batch_size` down to 1 splits them into
    two separate embedding calls, so the first can succeed and the second can
    fail -- proving this leaves *zero* rows, not the one chunk that already
    succeeded, is the point of this test. A version that stored each batch as
    it succeeded would leave one chunk here and pass a weaker assertion; it
    must not pass this one.
    """
    calls = {"n": 0}

    class _FailsAfterFirstBatch:
        name = "flaky-for-test"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += 1
            if calls["n"] == 1:
                return [[0.0] * 1536 for _ in texts]
            raise RuntimeError("embedding backend unavailable")

    tiny_batches = get_settings().model_copy(
        update={"embedding_batch_size": 1, "embedding_retry_backoff_seconds": 0.0}
    )
    monkeypatch.setattr(ingest_module, "get_embedding_provider", lambda: _FailsAfterFirstBatch())
    monkeypatch.setattr(ingest_module, "get_settings", lambda: tiny_batches)

    # Created and committed in its own transaction, ahead of the one that
    # fails -- mirroring how the worker actually sees documents (created by
    # an earlier, already-committed upload request), and keeping this test
    # from depending on whether the failing transaction below rolls its own
    # writes back before recording `failed`.
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    with pytest.raises(RuntimeError):
        async with tenant_session(tenant_a) as session:
            await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    # One successful batch plus `embedding_max_retries` failed attempts on
    # the second batch -- proves the retry loop actually retried rather than
    # giving up (or succeeding) on the first failure.
    assert calls["n"] == 1 + tiny_batches.embedding_max_retries

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.FAILED
    assert await _chunk_count(owner_connection, document.id) == 0


async def test_worker_task_for_a_document_in_another_org_fails_without_ingesting(
    tenant_a, tenant_b, owner_connection
):
    """`ingest_document_task` is handed `organization_id` by the queue, not
    derived from the document row. If it ever looked the document's own
    organization up instead of scoping the lookup to the id it was actually
    given, this would silently ingest cross-tenant instead of raising."""
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    with pytest.raises(NotFoundError):
        await ingest_document_task(
            {},
            organization_id=str(tenant_b.organization_id),
            document_id=str(document.id),
        )

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.PENDING
    assert await _chunk_count(owner_connection, document.id) == 0


async def test_worker_task_ingests_a_real_document_end_to_end(tenant_a, tmp_path, monkeypatch):
    """The only other test that drives `ingest_document_task` fails at the
    RLS lookup before storage is ever read (see the cross-org test above),
    so the actual production entry point -- load bytes off disk under the
    same `organization_id/document_id` key `store_document_bytes` writes
    them under, ingest, land `ready` -- had no coverage. This is that path,
    isolated from the real (gitignored) `./var/uploads` the same way
    `tests/unit/test_storage.py` isolates it, via a monkeypatched
    `upload_dir` pointed at `tmp_path`.
    """
    isolated_settings = get_settings().model_copy(update={"upload_dir": str(tmp_path)})
    monkeypatch.setattr(storage_module, "get_settings", lambda: isolated_settings)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a, mime_type="text/markdown")
    storage_module.store_document_bytes(tenant_a.organization_id, document.id, _MARKDOWN)

    await ingest_document_task(
        {},
        organization_id=str(tenant_a.organization_id),
        document_id=str(document.id),
    )

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)
        rows = (
            (
                await session.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == document.id)
                )
            )
            .scalars()
            .all()
        )

    assert fresh.status == DocumentStatus.READY
    assert len(rows) > 0


async def test_worker_task_with_a_null_mime_type_fails_the_document_cleanly(
    tenant_a, tmp_path, monkeypatch
):
    """`Document.mime_type` is nullable (a row could reach the worker with
    none recorded); `ingest_document_task` falls back to `""` rather than
    raising itself, and `extract()` rejects that the same way it rejects
    any other unsupported type -- through the ordinary failed-status path,
    not a second one. Pins that fallback: deleting it in favour of passing
    `document.mime_type` directly would make this a `mypy --strict`
    failure (`str | None` where `str` is required), not just a behaviour
    change, but the runtime behaviour is worth pinning on its own.
    """
    isolated_settings = get_settings().model_copy(update={"upload_dir": str(tmp_path)})
    monkeypatch.setattr(storage_module, "get_settings", lambda: isolated_settings)

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a, mime_type=None)
    storage_module.store_document_bytes(tenant_a.organization_id, document.id, _MARKDOWN)

    with pytest.raises(UnsupportedDocumentType):
        await ingest_document_task(
            {},
            organization_id=str(tenant_a.organization_id),
            document_id=str(document.id),
        )

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.FAILED


async def test_a_genuine_database_error_during_replace_chunks_still_lands_failed(
    tenant_a, tenant_b, owner_connection
):
    """Tests 2 and 4 both raise Python-level errors (`UnsupportedDocumentType`,
    a stubbed embedding failure) that never touch the database. This is the
    scenario the second-session failure-recording design in `ingest_document`
    actually exists for: a genuine database error partway through the `try`
    block, which leaves `session`'s own transaction aborted rather than
    merely raising a Python exception.

    The collision is seeded directly through `owner_connection` (bypassing
    RLS and the ownership check `replace_chunks` itself does), under
    `tenant_b`'s real organization -- a real row, just not this document's
    owner's. `replace_chunks`' own `DELETE` is scoped to
    `document_id AND organization_id`, so it never touches this stray row
    (wrong org), but its `INSERT` collides with it anyway: the
    `(document_id, chunk_index)` unique constraint does not include
    `organization_id`.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    await owner_connection.execute(
        text(
            "INSERT INTO document_chunks "
            "(id, organization_id, document_id, chunk_index, content, token_count, "
            " embedding, embedding_model, metadata) "
            "VALUES (gen_random_uuid(), :organization_id, :document_id, 0, 'stray', 1, "
            " CAST(:embedding AS vector), 'hashing', '{}')"
        ),
        {
            "organization_id": tenant_b.organization_id,
            "document_id": document.id,
            "embedding": _vector_literal(),
        },
    )
    await owner_connection.commit()

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_a) as session:
            await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.FAILED
    assert fresh.error is not None


async def test_two_concurrent_ingests_of_one_document_leave_it_ready(
    tenant_a, owner_connection, monkeypatch
):
    """Two workers ingesting the *same* document at once must not leave it
    `failed` with a correct corpus underneath it.

    Without the per-document advisory lock in `ingest_document`, this is
    what happens: `replace_chunks` is delete-then-insert, and under READ
    COMMITTED the loser's `DELETE` cannot see the rows the winner inserted
    after the loser's statement snapshot was taken. It deletes nothing,
    collides on `uq_chunk_document_index`, and -- worse than the wasted
    work -- its independent failure session then overwrites the winner's
    committed `ready` with `failed`. The corpus is intact and the document
    is retrievable, but `_retrieve_context`'s readiness gate
    (`list_documents(status=READY, limit=1)`) sees no ready document, so
    for a single-document organization grounding stops entirely.

    The slow embedder is what makes the overlap real rather than
    hypothetical: both jobs are inside their `try` block, past
    `mark_processing`, when the first one reaches `replace_chunks`. It must
    *not* be a two-party barrier -- under the fix the second job never
    reaches the embedder until the first has finished, which a barrier
    would deadlock on.
    """
    real_embedder = ingest_module.get_embedding_provider()

    class _SlowEmbedder:
        name = real_embedder.name

        async def embed(self, texts: list[str]) -> list[list[float]]:
            await asyncio.sleep(0.25)
            return await real_embedder.embed(texts)

    monkeypatch.setattr(ingest_module, "get_embedding_provider", lambda: _SlowEmbedder())

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    async def _run() -> int:
        async with tenant_session(tenant_a) as session:
            result = await ingest_document(
                session, tenant_a, document.id, _MARKDOWN, "text/markdown"
            )
            return result.chunk_count

    outcomes = await asyncio.gather(_run(), _run(), return_exceptions=True)

    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert outcomes[0] == outcomes[1]

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.READY
    assert fresh.error is None
    # Exactly one job's worth of chunks: the second ingest replaced the
    # first's rows rather than appending to or colliding with them.
    assert await _chunk_count(owner_connection, document.id) == outcomes[0]


async def test_a_cancelled_ingest_records_failed_and_re_raises_the_cancellation(
    tenant_a, monkeypatch
):
    """arq enforces `job_timeout` (600s) by cancelling the running task,
    which raises `asyncio.CancelledError` -- a `BaseException`, so
    `except Exception` never sees it.

    Before this was handled, that meant the one failure mode the queue
    exists to contain (the spec's own 200-page PDF, whose extraction and
    embedding legitimately outrun the timeout) recorded no `failed` status
    at all: arq abandoned the job after `max_tries`, the row stayed
    `processing` forever, and `retry_document` rejects `processing` by
    design. Delete-and-re-upload or direct SQL were the only ways out.

    Cancellation must still propagate -- swallowing it would tell arq the
    job succeeded and would break every `wait_for`/shutdown path above this
    one -- so this asserts both halves: the row says `failed`, and the
    `CancelledError` still comes out.
    """
    started = asyncio.Event()

    class _HangingEmbedder:
        name = "hanging-for-test"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    monkeypatch.setattr(ingest_module, "get_embedding_provider", lambda: _HangingEmbedder())

    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    async def _run() -> None:
        async with tenant_session(tenant_a) as session:
            await ingest_document(session, tenant_a, document.id, _MARKDOWN, "text/markdown")

    task = asyncio.create_task(_run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with tenant_session(tenant_a) as session:
        fresh = await DocumentService(session, tenant_a).get(document.id)

    assert fresh.status == DocumentStatus.FAILED
    assert fresh.error is not None
    assert "cancel" in fresh.error.lower()
