"""Enqueue an ingest job onto arq's Redis queue.

A thin wrapper so a caller (Task 5's upload endpoint) depends on one
function instead of knowing arq's pool API, this app's Redis DSN shape, and
the job name string all at once.
"""

import uuid

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.tasks import ingest_document_task


async def enqueue_ingest(document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `ingest_document_task` for this document.

    Opens and closes its own connection pool per call rather than holding a
    long-lived one: enqueuing a job is rare enough (one upload) that pool
    reuse would add a lifecycle to manage for no measurable benefit.
    """
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(
            # arq derives a job's registered name from the function object's
            # own `__name__` (see `WorkerSettings.functions`), so this must
            # be that same name, not a copy of it -- a bare string literal
            # here would silently drift the moment the function is renamed,
            # and the failure is invisible: the job just piles up in Redis
            # with no worker listening for it.
            ingest_document_task.__name__,
            organization_id=str(organization_id),
            document_id=str(document_id),
        )
    finally:
        # `aclose()`, not the deprecated `close()`: redis-py 5.0.1+ emits a
        # `DeprecationWarning` from `close()` (see `redis/utils.py`'s
        # `deprecated_function` wrapper), invisible until something
        # actually calls this function for real -- which nothing did until
        # `tests/integration/test_enqueue_ingest_live.py`, and this
        # suite's `filterwarnings = ["error"]` (pyproject.toml) turns any
        # warning into a hard failure. Found by that live test, not by
        # `tests/unit/test_queue.py`'s monkeypatched pool, which was never
        # going to call a real close() method either way.
        await pool.aclose()
