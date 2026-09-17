import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.rag.storage import delete_document_bytes

# Ceiling on `list_documents`'s `limit`, independent of whatever a caller
# asks for. GraphQL's `documents(limit: Int)` (app/graphql/resolvers.py)
# takes this straight from an authenticated but otherwise untrusted caller,
# with no upper bound of its own -- an unclamped limit is one query away
# from a full-table scan-and-serialize for a caller that passes 1_000_000.
_MAX_LIST_LIMIT = 100


class DocumentService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(self, data: CreateDocumentInput) -> Document:
        document = Document(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            title=data.title,
            source_type=data.source_type,
            source_uri=data.source_uri,
            mime_type=data.mime_type,
            file_size=data.file_size,
            checksum=data.checksum,
            status=DocumentStatus.PENDING,
            uploaded_by=data.uploaded_by,
            metadata_=data.metadata,
        )
        self.session.add(document)
        await self.session.flush()
        return document

    async def get(self, document_id: uuid.UUID) -> Document:
        result = await self.session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.organization_id == self.tenant.organization_id,
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            # Cross-tenant lookups must fail the same way a nonexistent id
            # does -- see the identical note on ConversationService.get.
            raise NotFoundError("document not found")
        return document

    async def list_documents(
        self,
        *,
        status: DocumentStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        """`status`/`limit`/`offset` are optional and keyword-only so the
        existing no-argument call (every caller before Task 5's GraphQL
        `documents` query) keeps working unchanged.

        `limit` and `offset` are clamped, not trusted: an unbounded `limit`
        is a full-table read one query away (see `_MAX_LIST_LIMIT`), and a
        negative `offset` is not merely meaningless -- Postgres rejects
        `OFFSET -1` outright, which without this clamp reaches the database
        as a query error and surfaces as an unhandled 500, not a clean
        empty or first-page result.
        """
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(0, offset)
        query = select(Document).where(Document.organization_id == self.tenant.organization_id)
        if status is not None:
            query = query.where(Document.status == status)
        query = query.order_by(Document.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def find_by_checksum(self, checksum: str) -> Document | None:
        result = await self.session.execute(
            select(Document).where(
                Document.checksum == checksum,
                Document.organization_id == self.tenant.organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def mark_processing(self, document_id: uuid.UUID) -> Document:
        document = await self.get(document_id)
        document.status = DocumentStatus.PROCESSING
        document.error = None
        await self.session.flush()
        return document

    async def mark_ready(self, document_id: uuid.UUID) -> Document:
        document = await self.get(document_id)
        document.status = DocumentStatus.READY
        document.error = None
        document.processed_at = datetime.now(UTC)
        await self.session.flush()
        return document

    async def mark_failed(self, document_id: uuid.UUID, error: str) -> Document:
        document = await self.get(document_id)
        document.status = DocumentStatus.FAILED
        document.error = error
        document.processed_at = datetime.now(UTC)
        await self.session.flush()
        return document

    async def replace_chunks(
        self, document_id: uuid.UUID, chunks: list[ChunkInput]
    ) -> list[DocumentChunk]:
        """Delete a document's existing chunks and insert `chunks` in order.

        The `get()` above is load-bearing, not belt-and-braces: a Postgres FK
        check runs with elevated privileges and does not consult this
        session's RLS policy, so an INSERT here would happily attach a chunk
        to another org's document_id even though a plain SELECT under this
        tenant's RLS sees zero rows for it. This scoped ownership check is
        the only thing that turns a cross-tenant document_id into
        NotFoundError instead of a silently-written cross-tenant reference.
        See the matching note on ConversationService.create/record_usage.
        """
        await self.get(document_id)

        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.organization_id == self.tenant.organization_id,
            )
        )

        rows = [
            DocumentChunk(
                id=uuid7(),
                organization_id=self.tenant.organization_id,
                document_id=document_id,
                chunk_index=index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=chunk.embedding,
                embedding_model=chunk.embedding_model,
                metadata_=chunk.metadata,
            )
            for index, chunk in enumerate(chunks)
        ]
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def delete(self, document_id: uuid.UUID) -> None:
        document = await self.get(document_id)
        await self.session.delete(document)
        await self.session.flush()
        # Makes the delete authoritative for the uploaded bytes too, not
        # just the row -- without this, `{upload_dir}/{organization_id}
        # /{document_id}` outlives the document that named it, forever.
        # Runs after the row delete is flushed but still inside the
        # caller's open transaction: a failure here (a permissions error,
        # a locked file) rolls the row delete back with it via the
        # caller's own `tenant_session`, rather than leaving a database
        # that says "gone" while the file says otherwise.
        delete_document_bytes(self.tenant.organization_id, document_id)
