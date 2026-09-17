"""arq job functions. This module's function names are arq's job names --
`WorkerSettings.functions` registers them by identity, and `enqueue_ingest`
enqueues them by the matching string, so renaming a function here is a
breaking change to anything already queued under the old name.
"""

import uuid
from typing import Any

from app.core.tenancy import TenantContext, tenant_session
from app.documents.service import DocumentService
from app.rag.ingest import ingest_document
from app.rag.storage import load_document_bytes


async def ingest_document_task(
    ctx: dict[str, Any], *, organization_id: str, document_id: str
) -> None:
    """The arq entry point for document ingestion.

    Runs in a background worker process, not inside an HTTP request, so
    unlike `ingest_document` (which takes an already-open session) this
    function has to open its own tenant-scoped transaction end to end --
    there is no request here to have opened one for it. `ctx` is arq's
    per-job context (its Redis pool, job id, retry count, ...); this job
    does not need anything out of it, but arq always calls a job function
    with it as the first positional argument.

    The document is looked up under `organization_id`'s RLS *before*
    anything is read from disk. That ordering matters: a job enqueued (or
    tampered with) against the wrong organization for this `document_id`
    finds no row here and fails right here with `NotFoundError`, rather than
    reading another tenant's bytes off disk and ingesting them under the
    wrong organization's chunks.
    """
    tenant = TenantContext(
        organization_id=uuid.UUID(organization_id),
        user_id=None,
        role=None,
        request_id=f"ingest:{document_id}",
    )
    document_uuid = uuid.UUID(document_id)
    async with tenant_session(tenant) as session:
        document = await DocumentService(session, tenant).get(document_uuid)
        # `mime_type` is nullable on the row (see app/db/models/document.py);
        # falling back to "" rather than raising here lets `extract()` reject
        # it the same way it rejects any other unsupported type, going
        # through the ordinary failed-status path instead of a second one.
        data = load_document_bytes(tenant.organization_id, document_uuid)
        await ingest_document(session, tenant, document_uuid, data, document.mime_type or "")
