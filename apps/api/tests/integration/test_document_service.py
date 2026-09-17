import pytest
from sqlalchemy import text

from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.db.models import DocumentSourceType, DocumentStatus
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService

pytestmark = pytest.mark.anyio


def _vector(seed: float = 0.0) -> list[float]:
    return [seed] * 1536


async def _document(session, tenant, **overrides):
    fields: dict[str, object] = {
        "title": "Refund Policy",
        "source_type": DocumentSourceType.TEXT,
        **overrides,
    }
    return await DocumentService(session, tenant).create(CreateDocumentInput(**fields))


async def test_create_returns_a_document_scoped_to_the_tenant(tenant_a):
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
    assert document.organization_id == tenant_a.organization_id
    assert document.status == DocumentStatus.PENDING


async def test_get_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await DocumentService(session, tenant_b).get(document.id)


async def test_list_documents_from_another_tenant_returns_empty(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await _document(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        documents = await DocumentService(session, tenant_b).list_documents()
    assert documents == []


async def test_find_by_checksum_scopes_to_the_tenant(tenant_a, tenant_b):
    """The same checksum under another org must not be found -- dedup is
    per-tenant, not global, or one org's upload would silently short-circuit
    another org's identical file."""
    async with tenant_session(tenant_a) as session:
        await _document(session, tenant_a, checksum="deadbeef")
    async with tenant_session(tenant_b) as session:
        found = await DocumentService(session, tenant_b).find_by_checksum("deadbeef")
    assert found is None

    async with tenant_session(tenant_a) as session:
        found = await DocumentService(session, tenant_a).find_by_checksum("deadbeef")
    assert found is not None
    assert found.checksum == "deadbeef"


async def test_replace_chunks_from_another_tenant_raises_not_found_and_writes_nothing(
    tenant_a, tenant_b, owner_connection
):
    """FK checks on document_chunks.document_id bypass the referencing
    session's RLS -- an INSERT carrying tenant_a's document id would succeed
    under tenant_b's RLS regardless. The scoped ownership SELECT inside
    replace_chunks is the only thing that can stop this, so this test proves
    both that it raises *and* that nothing lands in the table."""
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await DocumentService(session, tenant_b).replace_chunks(
                document.id,
                [
                    ChunkInput(
                        content="stolen chunk",
                        token_count=2,
                        embedding=_vector(),
                        embedding_model="hashing",
                    )
                ],
            )

    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM document_chunks WHERE document_id = :id"),
        {"id": document.id},
    )
    assert result.scalar_one() == 0


async def test_replace_chunks_assigns_sequential_chunk_index(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        chunks = await service.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content=f"chunk {i}",
                    token_count=2,
                    embedding=_vector(),
                    embedding_model="hashing",
                )
                for i in range(3)
            ],
        )
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]


async def test_replace_chunks_removes_the_previous_generation(tenant_a):
    """A re-ingest must not accumulate stale chunks alongside fresh ones."""
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        await service.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content="old", token_count=1, embedding=_vector(), embedding_model="hashing"
                )
            ],
        )
        chunks = await service.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content="new-1",
                    token_count=1,
                    embedding=_vector(),
                    embedding_model="hashing",
                ),
                ChunkInput(
                    content="new-2",
                    token_count=1,
                    embedding=_vector(),
                    embedding_model="hashing",
                ),
            ],
        )
    assert [chunk.content for chunk in chunks] == ["new-1", "new-2"]


async def test_deleting_a_document_cascades_its_chunks(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        await service.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content="chunk", token_count=1, embedding=_vector(), embedding_model="hashing"
                )
            ],
        )

    async with tenant_session(tenant_a) as session:
        await DocumentService(session, tenant_a).delete(document.id)

    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM document_chunks WHERE document_id = :id"),
        {"id": document.id},
    )
    assert result.scalar_one() == 0


async def test_mark_processing_then_ready_updates_status_and_processed_at(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        processing = await service.mark_processing(document.id)
        assert processing.status == DocumentStatus.PROCESSING

        ready = await service.mark_ready(document.id)
    assert ready.status == DocumentStatus.READY
    assert ready.processed_at is not None


async def test_mark_failed_records_the_error(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        failed = await service.mark_failed(document.id, "could not parse PDF")
    assert failed.status == DocumentStatus.FAILED
    assert failed.error == "could not parse PDF"
    assert failed.processed_at is not None


async def test_delete_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await DocumentService(session, tenant_b).delete(document.id)


async def test_metadata_round_trips(tenant_a):
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)
    assert document.metadata_ == {}


# ---------------------------------------------------------------------------
# Layer 1 in isolation
# ---------------------------------------------------------------------------


async def _unscoped_session():  # type: ignore[no-untyped-def]
    """A session that bypasses RLS entirely, for isolating Layer 1.

    Same pattern (and same reasoning) as
    `test_retrieve.py::test_organization_id_predicate_holds_even_when_rls_is_bypassed`:
    `app_owner` is the migration role, which owns these tables and therefore
    bypasses its own RLS policies, and no `app.current_org_id` is ever set on
    it. A merely *unscoped* session would not serve -- RLS's own
    `NULLIF(current_setting(...), \'\')::uuid` guard fails closed to zero rows
    when the setting is missing, so the assertions below would pass with the
    application-layer predicate deleted. Only a genuinely RLS-bypassing
    session can tell "Layer 1 filters" apart from "Layer 2 was doing all the
    work".
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_layer_1_predicates_hold_even_when_rls_is_bypassed(tenant_a, tenant_b):
    """`docs/ARCHITECTURE.md` §2.3's Layer 1, on the three `DocumentService`
    queries that RLS alone was covering.

    Every other test in this file runs under an RLS-scoped session, where
    Layer 2 already stops a cross-tenant read -- which cannot distinguish an
    explicit `organization_id` predicate from no predicate at all. All three
    of these were mutation-verified deletable with the suite green:
    `find_by_checksum` (32 passed), `list_documents` (26 passed) and the
    GraphQL `chunkCount` loader (14 passed).

    `find_by_checksum` is the one worth naming: the pre-flight notes claimed
    `test_find_by_checksum_scopes_to_the_tenant` pinned it. It does not --
    that test runs under `tenant_b`'s own RLS-scoped session, where org A's
    row is invisible whatever the service does.
    """
    from app.graphql.context import Context

    checksum = "a" * 64
    async with tenant_session(tenant_b) as session:
        other = await _document(session, tenant_b, title="Org B doc", checksum=checksum)
        await DocumentService(session, tenant_b).replace_chunks(
            other.id,
            [
                ChunkInput(
                    content="org b chunk",
                    token_count=3,
                    embedding=_vector(0.5),
                    embedding_model="hashing",
                )
            ],
        )

    engine, session_factory = await _unscoped_session()
    try:
        async with session_factory() as unscoped:
            service = DocumentService(unscoped, tenant_a)

            # 1. find_by_checksum: org B's row has this exact checksum, and
            # without the predicate this returns it -- which would then make
            # the upload endpoint answer org A with org B's document.
            assert await service.find_by_checksum(checksum) is None

            # 2. list_documents: org B's document must not appear in org A's
            # list, with or without a status filter.
            assert await service.list_documents() == []
            assert await service.list_documents(status=DocumentStatus.PENDING) == []

            # 3. the GraphQL `chunkCount` loader, asked directly about org
            # B's document id: it must count zero of org B's chunks for org
            # A, not one.
            context = Context(tenant=tenant_a, session=unscoped)
            assert await context._load_chunk_counts([other.id]) == [0]
    finally:
        await engine.dispose()


async def test_two_documents_in_one_org_cannot_share_a_checksum(tenant_a, tenant_b):
    """`find_by_checksum` is the upload endpoint's idempotence check, and a
    check-then-insert is not idempotent on its own: two concurrent uploads
    of identical bytes both find nothing, both create a row, both enqueue a
    job and both get billed for embedding the same content -- against §3's
    idempotence claim. The UI gates a double-click, so this needs two tabs
    or an API client, which is to say it needs a constraint rather than a
    convention.

    Partial, on `checksum IS NOT NULL`: a document created by any path that
    records no checksum (`source_type` of `text`/`url`, every fixture in
    this suite) must not collide with every other such document.
    """
    from sqlalchemy.exc import IntegrityError

    checksum = "b" * 64

    async with tenant_session(tenant_a) as session:
        await _document(session, tenant_a, checksum=checksum)

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_a) as session:
            await _document(session, tenant_a, checksum=checksum)

    # Scoped to the organization: the same bytes uploaded by a different
    # tenant are a different document, and must not be refused.
    async with tenant_session(tenant_b) as session:
        other = await _document(session, tenant_b, checksum=checksum)
    assert other.organization_id == tenant_b.organization_id


async def test_documents_with_no_checksum_are_not_constrained(tenant_a):
    async with tenant_session(tenant_a) as session:
        first = await _document(session, tenant_a)
        second = await _document(session, tenant_a)
    assert first.checksum is None and second.checksum is None
