"""Database-level behaviour that no amount of ORM-level testing proves: the
pgvector column round-trips real floats, and the generated tsvector column
is populated by Postgres itself, under the 'english' configuration Task 6's
retrieval queries depend on.
"""

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import tenant_session
from app.db.models import DocumentSourceType
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService

pytestmark = pytest.mark.anyio


async def _document(session, tenant):
    return await DocumentService(session, tenant).create(
        CreateDocumentInput(title="Handbook", source_type=DocumentSourceType.TEXT)
    )


async def test_embedding_round_trips_through_the_vector_column(tenant_a):
    # Not all-zero and not uniform: a bug that transposed or truncated the
    # array would still pass an all-zero-vector comparison.
    vector = [((i % 11) - 5) / 5.0 for i in range(1536)]
    async with tenant_session(tenant_a) as session:
        service = DocumentService(session, tenant_a)
        document = await _document(session, tenant_a)
        chunks = await service.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content="a chunk with a real embedding",
                    token_count=6,
                    embedding=vector,
                    embedding_model="hashing",
                )
            ],
        )
    assert chunks[0].embedding == pytest.approx(vector)


async def test_content_tsv_is_populated_by_the_database(tenant_a, owner_connection):
    """Inserts with raw SQL, bypassing DocumentChunk entirely, so this proves
    the generated column -- not any ORM-side default -- populates content_tsv,
    and that it does so under the 'english' configuration rather than
    'simple' or the server default. A wrong configuration here would still
    leave content_tsv non-empty, so the test also runs the exact
    `websearch_to_tsquery('english', ...)` shape Task 6's retrieval uses.
    """
    async with tenant_session(tenant_a) as session:
        document = await _document(session, tenant_a)

    chunk_id = uuid7()
    zero_vector = "[" + ",".join(["0"] * 1536) + "]"
    await owner_connection.execute(
        text(
            "INSERT INTO document_chunks "
            "(id, organization_id, document_id, chunk_index, content, token_count, "
            "embedding, embedding_model, metadata) "
            "VALUES (:id, :org_id, :doc_id, 0, :content, 4, CAST(:embedding AS vector), "
            "'hashing', '{}')"
        ),
        {
            "id": chunk_id,
            "org_id": tenant_a.organization_id,
            "doc_id": document.id,
            "content": "Our refund policy covers returns within thirty days",
            "embedding": zero_vector,
        },
    )
    await owner_connection.commit()

    populated = await owner_connection.execute(
        text("SELECT content_tsv IS NOT NULL FROM document_chunks WHERE id = :id"),
        {"id": chunk_id},
    )
    assert populated.scalar_one() is True

    matched = await owner_connection.execute(
        text(
            "SELECT content_tsv @@ websearch_to_tsquery('english', 'refund') "
            "FROM document_chunks WHERE id = :id"
        ),
        {"id": chunk_id},
    )
    assert matched.scalar_one() is True
