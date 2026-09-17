"""`POST /api/v1/documents` -- multipart upload -- and
`POST /api/v1/documents/{id}/retry`.

REST rather than GraphQL for the upload itself: GraphQL has no multipart
upload spec worth adopting here (the `graphql-multipart-request-spec`
convention is a client-side hack, not something graphql-core or strawberry
speak natively), and a single-file form post is exactly what plain HTTP
multipart already does well. GraphQL keeps the read side (`documents`,
`document`, `deleteDocument` -- see `app/graphql/resolvers.py`).

Because this hand-rolls `Request.form()` instead of using FastAPI's
`File()`/`Form()` parameters (required for the streaming size cap below --
see `_capped_receive`), this route has no generated request schema and does
not appear in `/docs` at all. The dashboard team integrating against it
gets nothing from the OpenAPI page for this one endpoint; this module's
docstrings and the Task 5 report are the only spec.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from starlette.datastructures import UploadFile
from starlette.types import Message, Receive

from app.auth.dependencies import get_current_tenant
from app.core.config import get_settings
from app.core.errors import ConflictError, PayloadTooLargeError, ValidationError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Document as DocumentModel
from app.db.models import DocumentSourceType, DocumentStatus
from app.documents.schemas import CreateDocumentInput
from app.documents.service import DocumentService
from app.rag.extract import SUPPORTED_MIME_TYPES, UnsupportedDocumentType, resolve_mime_type
from app.rag.queue import enqueue_ingest
from app.rag.storage import store_document_bytes

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


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


def _capped_receive(receive: Receive, limit: int) -> Receive:
    """Wrap an ASGI `receive` callable so the request body is capped
    mid-stream, before Starlette's multipart parser ever sees a byte past
    the limit.

    This is what actually closes the gap a post-read `len(data)` check and
    a `Content-Length` pre-check both leave open: a client that sends no
    `Content-Length` (chunked transfer) or lies about it. Starlette's own
    `MultiPartParser` cannot be asked to enforce this instead -- reading
    `on_part_data` in `starlette/formparsers.py`, its `max_part_size`
    ceiling is only ever checked for a part with no `.file` set (a plain
    form field like `title`); once a part has a filename, that branch
    never runs, and the file is written to its `SpooledTemporaryFile` with
    no size limit at all. Counting bytes as ASGI actually delivers them,
    here, is the one place still inside this process that sees every byte
    before anything downstream (the parser, `SpooledTemporaryFile`, this
    endpoint's own `await upload.read()`) gets to keep any of them.
    """
    total = 0

    async def wrapper() -> Message:
        nonlocal total
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                raise PayloadTooLargeError(f"upload exceeds the {limit} byte limit")
        return message

    return wrapper


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> DocumentResponse:
    settings = get_settings()

    # Layer 1: reject a declared-oversized upload from its Content-Length
    # header alone, before `request.form()` is ever awaited. This is the
    # cheapest rejection -- no receive() call happens at all -- and covers
    # every well-behaved client, which is to say nearly all of them: a
    # multipart body built from bytes/a file already on disk has a known
    # length, and every HTTP client I'm aware of sends it.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > settings.max_request_bytes:
            raise PayloadTooLargeError(
                f"upload exceeds the {settings.max_request_bytes} byte limit"
            )

    # Layer 2: cap the actual byte stream, regardless of what
    # Content-Length claimed (or omitted). A fresh `Request` bound to the
    # same ASGI `scope` but a wrapped `receive` -- `request.form()` reads
    # through `self._receive`, set from the `receive` passed to
    # `Request.__init__`, so this is the whole mechanism; nothing about
    # the scope, headers, or client changes. `PayloadTooLargeError` raised
    # from inside `wrapper()` propagates straight out of the multipart
    # parser's `async for chunk in self.stream` loop (it closes any
    # partially-written files on the way out; see `MultiPartParser.parse`'s
    # `except BaseException` in `starlette/formparsers.py`) and out of
    # `request.form()` itself, before this line ever returns.
    capped_request = Request(
        request.scope, _capped_receive(request.receive, settings.max_request_bytes)
    )

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
    async with capped_request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValidationError("a 'file' part is required")

        # No separate post-read `len(data)` check here: Layer 2 above
        # already guarantees the whole request body -- and therefore this
        # file part -- never exceeded `max_request_bytes` by the time
        # `.read()` can return at all. A third check computing the same
        # fact `wrapper()` already enforced mid-stream would be dead code,
        # not defense in depth.
        data = await upload.read()

        # `extract.SUPPORTED_MIME_TYPES` is the single definition of "can we
        # ingest this" -- see that module's docstring. Checking it here,
        # against the same frozenset the worker's `extract()` itself raises
        # `UnsupportedDocumentType` from, is what keeps a file that uploads
        # cleanly from ever failing later for a type this module was never
        # taught to handle.
        # Resolved, not taken at face value: the browser reports `""` for
        # `.md` on any OS without that registry association, and the
        # resolved value is what gets *stored* on the row -- the worker
        # reads `documents.mime_type` back to decide how to extract, so
        # admitting a file here and storing `""` would only move the failure
        # into the worker. See `resolve_mime_type`.
        mime_type = resolve_mime_type(upload.content_type, upload.filename)
        if mime_type not in SUPPORTED_MIME_TYPES:
            # Logged, not just raised: a rejection the user sees as "not a
            # supported file type" is the one case where the operator needs
            # to know *what* was offered, because the usual cause is a
            # browser reporting a type this list has not been taught.
            logger.info(
                "document_upload_rejected",
                organization_id=str(tenant.organization_id),
                reason="unsupported_mime_type",
                mime_type=mime_type,
                file_size=len(data),
            )
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
            logger.info(
                "document_upload_deduplicated",
                organization_id=str(tenant.organization_id),
                document_id=str(existing.id),
                file_size=len(data),
            )
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

    # Runs only once the block above has actually exited -- the point at
    # which `tenant_session` commits. `ingest_document` (run by the worker
    # this enqueues) looks the document up through its own, independent
    # session/connection; that lookup cannot see a row this request has
    # only flushed, not committed. Enqueueing any earlier risks the
    # worker's `mark_processing` racing ahead of this commit and raising
    # NotFoundError from outside its own try block -- which the pipeline
    # has no way to turn into a recorded `failed` status. See the module
    # docstring on `app/rag/ingest.py`.
    #
    # Called as the bare module global, not through a `Depends` seam: a
    # test that wants this off the network monkeypatches
    # `documents_api.enqueue_ingest` directly
    # (`monkeypatch.setattr(documents_api, "enqueue_ingest", fake)`), which
    # Python's name resolution honours here with no indirection required --
    # this reference is looked up in the module's globals afresh on every
    # call, exactly like any other module-level function call.
    await enqueue_ingest(response.id, tenant.organization_id)
    # Nothing here carries the title or any of the bytes: the operator needs
    # to know a document of this size and type was accepted for this
    # organization, not what is in it.
    logger.info(
        "document_upload_accepted",
        organization_id=str(tenant.organization_id),
        document_id=str(response.id),
        mime_type=mime_type,
        file_size=len(data),
    )
    return response


@router.post("/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_document(
    document_id: uuid.UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> DocumentResponse:
    async with tenant_session(tenant) as session:
        # Tenant-scoped: a cross-org document_id raises NotFoundError here
        # exactly the way a nonexistent one does (see DocumentService.get),
        # so org B retrying org A's document answers 404, not 403 -- it
        # never learns the id exists at all.
        service = DocumentService(session, tenant)
        document = await service.get(document_id)
        previous_status = document.status

        # `failed` and `pending` may both be retried; `processing` and
        # `ready` are rejected.
        #
        # `ready` is rejected because re-running a succeeded ingest is
        # pointless and doubles the embedding bill for nothing.
        #
        # `pending` is retryable -- and this is not the original reasoning
        # here, which claimed it already has a job in flight and was wrong:
        # `pending` means "the row was committed; a job may or may not have
        # been enqueued for it," not "a job exists." `enqueue_ingest` runs
        # *after* the upload's own commit (see `upload_document` above), so
        # any failure in that call -- Redis down, a network blip, the
        # process dying between the commit and the enqueue -- strands the
        # document in `pending` forever with no job anywhere and no way
        # back: retrying it was rejected by this exact check, and
        # re-uploading identical bytes hits the checksum dedup and enqueues
        # nothing either. Both of this document's only two ways out were
        # closed. Allowing retry on `pending` reopens one. Re-enqueueing a
        # document that in fact already has a job in flight is safe
        # because ingestion is idempotent at the point that matters:
        # `replace_chunks` deletes the document's existing chunks before
        # inserting the new ones (see `DocumentService.replace_chunks`), so
        # two jobs racing on the same document produce one duplicate
        # (wasted) embedding call, not a doubled corpus -- annoying, never
        # corrupting.
        #
        # `processing` is still rejected: unlike `pending`, it is only ever
        # set by `ingest_document` itself, from inside the worker that is
        # actively running it (see `mark_processing` in
        # `app/rag/ingest.py`) -- so, unlike `pending`, this status is
        # genuine proof a worker currently holds the document, not merely a
        # maybe. Retrying it would race that worker on purpose instead of
        # recovering from a job that may never have existed.
        if document.status not in (DocumentStatus.FAILED, DocumentStatus.PENDING):
            raise ConflictError(
                f"cannot retry a document with status '{document.status.value}'; "
                "only a failed or pending document may be retried"
            )

        # Accepting a retry has to move the row, not merely enqueue a job.
        # Left on `failed`, the client's refetch sees `FAILED`,
        # `shouldPollDocuments` (apps/web/src/lib/document-status.ts) treats
        # that as terminal and never starts its timer, and the row sits on
        # "Failed" with the previous attempt's error message while the
        # worker quietly takes it through `processing` to `ready` -- until
        # someone reloads the page by hand. `pending` is both the honest
        # description of "accepted, not started" and a non-terminal status,
        # so polling resumes on its own.
        document = await service.mark_pending(document_id)
        response = DocumentResponse.from_model(document)
        logger.info(
            "document_retry_accepted",
            organization_id=str(tenant.organization_id),
            document_id=str(document_id),
            previous_status=previous_status.value,
        )

    await enqueue_ingest(document_id, tenant.organization_id)
    return response
