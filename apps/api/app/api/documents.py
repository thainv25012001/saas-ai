"""`POST /api/v1/documents` -- multipart upload -- and
`POST /api/v1/documents/{id}/retry`.

REST rather than GraphQL for the upload itself: GraphQL has no multipart
upload spec worth adopting here (the `graphql-multipart-request-spec`
convention is a client-side hack, not something graphql-core or strawberry
speak natively), and a single-file form post is exactly what plain HTTP
multipart already does well. GraphQL keeps the read side (`documents`,
`document`, `deleteDocument` -- see `app/graphql/resolvers.py`).
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from app.auth.dependencies import get_current_tenant
from app.core.config import get_settings
from app.core.errors import ConflictError, PayloadTooLargeError, ValidationError
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Document as DocumentModel
from app.db.models import DocumentSourceType, DocumentStatus
from app.documents.schemas import CreateDocumentInput
from app.documents.service import DocumentService
from app.rag.extract import SUPPORTED_MIME_TYPES, UnsupportedDocumentType
from app.rag.queue import enqueue_ingest
from app.rag.storage import store_document_bytes

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# The type both routes below depend on, and the seam their tests
# monkeypatch (`monkeypatch.setattr(documents_api, "enqueue_ingest", fake)`)
# to keep this module off Redis entirely -- matching
# `app.api.chat.get_chat_provider`'s own pattern for keeping a route's tests
# off the network. A thin `Depends` wrapper rather than calling
# `enqueue_ingest` as a bare module global directly: FastAPI resolves the
# dependency fresh on every request, so a test that patches this module's
# `enqueue_ingest` name is picked up immediately, with no app rebuild and no
# `dependency_overrides` bookkeeping required.
EnqueueIngest = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


def get_enqueue_ingest() -> EnqueueIngest:
    return enqueue_ingest


class DocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    status: DocumentStatus
    mime_type: str | None
    file_size: int | None
    checksum: str | None
    error: str | None
    created_at: datetime
    processed_at: datetime | None

    @classmethod
    def from_model(cls, document: DocumentModel) -> "DocumentResponse":
        return cls(
            id=document.id,
            title=document.title,
            status=document.status,
            mime_type=document.mime_type,
            file_size=document.file_size,
            checksum=document.checksum,
            error=document.error,
            created_at=document.created_at,
            processed_at=document.processed_at,
        )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    enqueue: Annotated[EnqueueIngest, Depends(get_enqueue_ingest)],
) -> DocumentResponse:
    settings = get_settings()

    # Reject a declared-oversized upload from its Content-Length header
    # alone, before `request.form()` is ever awaited. This is the only
    # enforcement point that costs nothing: no body byte is read, so
    # nothing is copied into a Python object and nothing is written to the
    # multipart parser's own spooled temp file either.
    #
    # It does not close every path to a 2GB upload, though, and that gap is
    # deliberately not hidden: a client that sends no Content-Length
    # (chunked transfer) or lies about it defeats this check entirely, and
    # is only caught by the `len(data)` check below -- by which point
    # Starlette's `MultiPartParser` has already received and written the
    # whole file part to a `SpooledTemporaryFile` (disk beyond 1MB, not
    # this process's RAM, but not free either; see
    # `starlette.formparsers.MultiPartParser`). That parser's own
    # `max_part_size` cannot be pointed at this instead -- reading its
    # `on_part_data`, that ceiling is only ever checked for a part with no
    # `file` set, i.e. a plain form field like `title`, never a part that
    # already has a filename and therefore a `SpooledTemporaryFile`
    # target. Closing the gap for real needs a parser that enforces a byte
    # ceiling on the file part itself, mid-stream; nothing in this
    # dependency stack currently offers that. See the Task 5 report for the
    # full reasoning.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > settings.max_upload_bytes:
            raise PayloadTooLargeError(f"upload exceeds the {settings.max_upload_bytes} byte limit")

    # `async with`, not a bare `await`: `Request.form()` returns an
    # `AwaitableOrContextManagerWrapper`, and only the context-manager form
    # closes the `UploadFile`(s) it creates on the way out --
    # `FormData.close()` -- regardless of whether the block below returns
    # normally or raises. FastAPI's own `File()`/`Form()` params get this
    # for free (their dependency-solving machinery calls `request.close()`
    # for you); calling `request.form()` directly, as this does, does not,
    # and a plain `await request.form()` here left every request's
    # `SpooledTemporaryFile` unclosed until GC got around to it -- a real
    # file-descriptor leak under load, and loud as a `ResourceWarning` in
    # this test suite once warnings are treated as errors.
    async with request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValidationError("a 'file' part is required")

        data = await upload.read()
        if len(data) > settings.max_upload_bytes:
            raise PayloadTooLargeError(f"upload exceeds the {settings.max_upload_bytes} byte limit")

        # `extract.SUPPORTED_MIME_TYPES` is the single definition of "can we
        # ingest this" -- see that module's docstring. Checking it here,
        # against the same frozenset the worker's `extract()` itself raises
        # `UnsupportedDocumentType` from, is what keeps a file that uploads
        # cleanly from ever failing later for a type this module was never
        # taught to handle.
        mime_type = upload.content_type or ""
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise UnsupportedDocumentType(f"unsupported document type '{mime_type}'")

        title_field = form.get("title")
        title = str(title_field).strip() if title_field else ""
        if not title:
            title = upload.filename or "untitled"

    checksum = hashlib.sha256(data).hexdigest()

    async with tenant_session(tenant) as session:
        service = DocumentService(session, tenant)
        existing = await service.find_by_checksum(checksum)
        if existing is not None:
            # Dedup: identical bytes already exist for this org (ingested,
            # or already mid-ingest from an earlier upload of the same
            # file). Returning that row -- and doing nothing else -- is the
            # whole point: enqueueing again would be a second embedding
            # bill for content this org already paid to embed once.
            return DocumentResponse.from_model(existing)

        document = await service.create(
            CreateDocumentInput(
                title=title[:255],
                source_type=DocumentSourceType.UPLOAD,
                mime_type=mime_type,
                file_size=len(data),
                checksum=checksum,
                uploaded_by=tenant.user_id,
            )
        )
        # Written to disk while the row's transaction is still open: if
        # this raises, the `async with` block below rolls the row back with
        # it, instead of leaving a committed `pending` document with no
        # bytes behind it for the worker to ever find.
        store_document_bytes(tenant.organization_id, document.id, data)
        response = DocumentResponse.from_model(document)

    # `enqueue` runs only once the block above has actually exited -- the
    # point at which `tenant_session` commits. `ingest_document` (run by the
    # worker this enqueues) looks the document up through its own,
    # independent session/connection; that lookup cannot see a row this
    # request has only flushed, not committed. Enqueueing any earlier risks
    # the worker's `mark_processing` racing ahead of this commit and raising
    # NotFoundError from outside its own try block -- which the pipeline has
    # no way to turn into a recorded `failed` status. See the module
    # docstring on `app/rag/ingest.py`.
    await enqueue(response.id, tenant.organization_id)
    return response


@router.post("/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_document(
    document_id: uuid.UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    enqueue: Annotated[EnqueueIngest, Depends(get_enqueue_ingest)],
) -> DocumentResponse:
    async with tenant_session(tenant) as session:
        # Tenant-scoped: a cross-org document_id raises NotFoundError here
        # exactly the way a nonexistent one does (see DocumentService.get),
        # so org B retrying org A's document answers 404, not 403 -- it
        # never learns the id exists at all.
        document = await DocumentService(session, tenant).get(document_id)

        # Only `failed` may be retried. `ready` is rejected because
        # re-running a succeeded ingest is pointless and doubles the
        # embedding bill for nothing. `pending` and `processing` are
        # rejected on the same logic as the dedup check in
        # `upload_document` above: both already have exactly one job in
        # flight for this document (the one the original upload enqueued,
        # or an earlier retry), and enqueueing a second one risks two
        # workers running `replace_chunks` for the same document
        # concurrently -- last writer wins on the chunks, and the loser's
        # embedding call was still a real charge for nothing kept. `failed`
        # is the only status where a second job is safe to add: the first
        # one is unambiguously finished (loudly, with an error already on
        # the row) and there is nothing in flight left to race with.
        if document.status != DocumentStatus.FAILED:
            raise ConflictError(
                f"cannot retry a document with status '{document.status.value}'; "
                "only a failed document may be retried"
            )
        response = DocumentResponse.from_model(document)

    await enqueue(document_id, tenant.organization_id)
    return response
