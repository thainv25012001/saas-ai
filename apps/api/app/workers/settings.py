"""arq worker configuration: which jobs it runs and how it retries them.

An arq worker started with an empty `functions` list boots without
complaint and then processes nothing forever -- there is no error, just
silence. `tests/unit/test_worker_settings.py` pins `ingest_document_task`
actually being in this list for exactly that reason.
"""

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.tasks import ingest_document_task


class WorkerSettings:
    functions = [ingest_document_task]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Retries at the arq job level, on top of `_embed_all`'s own per-batch
    # retries inside `ingest_document` -- this covers failures the pipeline
    # itself cannot retry into success, like the worker process being killed
    # mid-job.
    max_tries = 3
    # A 200-page PDF, batched embedding calls and their retries can
    # legitimately run for minutes; 600s gives that room without letting a
    # truly stuck job hold a worker slot forever.
    job_timeout = 600
