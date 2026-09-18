"""Queueing a document for ingestion."""

import uuid

from app.workers.enqueue import enqueue
from app.workers.tasks import ingest_document_task


async def enqueue_ingest(document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `ingest_document_task` for this document."""
    await enqueue(
        ingest_document_task,
        organization_id=str(organization_id),
        document_id=str(document_id),
    )
