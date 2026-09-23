"""`POST /api/v1/products/import` -- multipart CSV/XLSX/JSON catalogue upload
for Task 3 (`docs/PHASE-5.md` §5), `GET /api/v1/products/import/template`
for a sample file to start from -- and `GET /api/v1/products/import/{id}`, so a
customer can come back later and see why row 12 failed. That second route
is not optional decoration: the import runs in an arq job well after this
module has answered 202, so the counts and per-row errors the brief
requires only mean anything if something durable can be re-read after the
fact -- see `app/db/models/product_import.py` for why that "something" is a
real table, not this response.

Reuses Phase 3's upload machinery wholesale rather than re-deriving it: the
capped ASGI `receive` (`app.api.documents._capped_receive`) that bounds the
request body mid-stream regardless of what Content-Length claims or omits,
and the upload -> 202 -> arq job flow generally. See that module's
docstring for the reasoning this one does not repeat.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from app.api.documents import _capped_receive
from app.auth.dependencies import get_current_tenant
from app.core.config import get_settings
from app.core.errors import PayloadTooLargeError, ValidationError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ProductImport as ProductImportModel
from app.db.models import ProductImportStatus
from app.products.import_template import TemplateFormat, build_import_template
from app.products.importer import (
    SUPPORTED_IMPORT_MIME_TYPES,
    ProductImportService,
    UnsupportedImportType,
    enqueue_product_import,
    resolve_import_mime_type,
    store_import_bytes,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/products", tags=["products"])


class ProductImportRowError(BaseModel):
    row: int
    external_id: str | None
    message: str


class ProductImportResponse(BaseModel):
    id: uuid.UUID
    status: ProductImportStatus
    filename: str | None
    mime_type: str | None
    total_rows: int | None
    succeeded_count: int
    failed_count: int
    errors: list[ProductImportRowError]
    error: str | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_model(cls, record: ProductImportModel) -> "ProductImportResponse":
        return cls(
            id=record.id,
            status=record.status,
            filename=record.filename,
            mime_type=record.mime_type,
            total_rows=record.total_rows,
            succeeded_count=record.succeeded_count,
            failed_count=record.failed_count,
            errors=[ProductImportRowError(**row_error) for row_error in record.errors],
            error=record.error,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_products(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> ProductImportResponse:
    settings = get_settings()

    # Layer 1 + Layer 2 of the same size guard `upload_document` uses --
    # see app/api/documents.py's docstrings on `upload_document` and
    # `_capped_receive` for why both checks exist and neither subsumes the
    # other.
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

    capped_request = Request(
        request.scope, _capped_receive(request.receive, settings.max_request_bytes)
    )

    async with capped_request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValidationError("a 'file' part is required")

        data = await upload.read()

        # Resolved, not taken at face value -- same reasoning as
        # `upload_document`'s identical check: a browser that reports
        # nothing useful for `.csv`/`.json` (empty string, or
        # `application/octet-stream`) still gets a real content type
        # recorded on the row, so the worker's `parse_import` never fails
        # for a type this door already believed it was letting through.
        mime_type = resolve_import_mime_type(upload.content_type, upload.filename)
        if mime_type not in SUPPORTED_IMPORT_MIME_TYPES:
            logger.info(
                "product_import_rejected",
                organization_id=str(tenant.organization_id),
                reason="unsupported_mime_type",
                mime_type=mime_type,
                file_size=len(data),
            )
            raise UnsupportedImportType(f"unsupported import type '{mime_type}'")

        # `product_imports.filename` is `String(255)`; a longer upload name
        # would fail the INSERT with a 500 after the bytes were read. Cut to
        # fit, the way `upload_document` cuts a document title.
        filename = upload.filename[:255] if upload.filename else upload.filename

    async with tenant_session(tenant) as session:
        record = await ProductImportService(session, tenant).create(
            filename=filename, mime_type=mime_type
        )
        # Written while the row's own transaction is still open, exactly
        # like `upload_document`: if this raises, the block below rolls the
        # row back with it instead of leaving a committed `pending` import
        # with no bytes behind it for the worker to ever find.
        store_import_bytes(tenant.organization_id, record.id, data)
        response = ProductImportResponse.from_model(record)

    # After the commit, not before -- `import_products_task` looks this row
    # up through its own, independent session/connection, which cannot see
    # a row only flushed and not yet committed. See `upload_document`'s
    # identical comment for the race this ordering avoids.
    await enqueue_product_import(response.id, tenant.organization_id)
    logger.info(
        "product_import_accepted",
        organization_id=str(tenant.organization_id),
        product_import_id=str(response.id),
        mime_type=mime_type,
        file_size=len(data),
    )
    return response


@router.get("/import/template", dependencies=[Depends(get_current_tenant)])
async def download_import_template(
    fmt: Annotated[TemplateFormat, Query(alias="format")] = "csv",
) -> Response:
    """A sample catalogue in one of the formats `import_products` accepts,
    with every column it reads -- see `app/products/import_template.py`.
    Registered before `/import/{product_import_id}` so "template" is never
    tried as an import id. Behind auth like the rest of the router, though
    the file holds nothing tenant-specific."""
    template = build_import_template(fmt)
    return Response(
        content=template.content,
        media_type=template.media_type,
        headers={"Content-Disposition": f'attachment; filename="{template.filename}"'},
    )


@router.get("/import/{product_import_id}")
async def get_product_import(
    product_import_id: uuid.UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> ProductImportResponse:
    async with tenant_session(tenant) as session:
        record = await ProductImportService(session, tenant).get(product_import_id)
        return ProductImportResponse.from_model(record)
