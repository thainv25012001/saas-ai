"""arq worker configuration: which jobs it runs and how it retries them.

An arq worker started with an empty `functions` list boots without
complaint and then processes nothing forever -- there is no error, just
silence. `tests/unit/test_worker_settings.py` pins `ingest_document_task`
actually being in this list for exactly that reason.
"""

from collections.abc import Callable
from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers.tasks import (
    import_products_task,
    ingest_document_task,
    title_conversation_task,
)


async def _on_startup(_ctx: dict[str, Any]) -> None:
    """Configure structlog in the worker process.

    `configure_logging` is called from `app/main.py` for the API, and this
    process never imports it -- so without this hook the worker ran on
    structlog's defaults: `LOG_LEVEL` ignored entirely, and console-shaped
    output where every other process in the deployment emits JSON. Two
    formats in one log pipeline is the kind of thing nobody notices until
    they are trying to find out why an ingest failed at 3am.
    """
    configure_logging(get_settings().log_level)


class WorkerSettings:
    # Annotated, not inferred: mypy infers a list's type from its first
    # element, so a second job with a different signature is a `list-item`
    # error rather than the heterogeneous registry arq actually wants.
    functions: list[Callable[..., Any]] = [
        ingest_document_task,
        title_conversation_task,
        import_products_task,
    ]
    on_startup = _on_startup
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
    # arq defaults `max_jobs` to 10 -- fine for jobs that hold at most one
    # connection each, wrong here: `ingest_document` holds up to two of this
    # process's own pool connections per job (see `settings.worker_max_jobs`
    # in app/core/config.py for the arithmetic against `pool_size` +
    # `max_overflow`). Left at arq's default, 10 concurrent jobs could want
    # 20 connections against a 15-connection pool -- not a deadlock (the
    # jobs that get a second connection finish and release, so the queue
    # always drains), but a `pool_timeout` (30s) stall on every job past
    # the first few that did not exist before this pipeline held two
    # connections at once.
    max_jobs = get_settings().worker_max_jobs
