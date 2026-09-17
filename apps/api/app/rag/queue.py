"""Enqueue an ingest job onto arq's Redis queue.

A thin wrapper so a caller (Task 5's upload endpoint) depends on one
function instead of knowing arq's pool API, this app's Redis DSN shape, and
the job name string all at once.
"""

import uuid

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings


async def enqueue_ingest(document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `ingest_document_task` for this document.

    Opens and closes its own connection pool per call rather than holding a
    long-lived one: enqueuing a job is rare enough (one upload) that pool
    reuse would add a lifecycle to manage for no measurable benefit.
    """
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(
            "ingest_document_task",
            organization_id=str(organization_id),
            document_id=str(document_id),
        )
    finally:
        await pool.close()
