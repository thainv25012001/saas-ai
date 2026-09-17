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
