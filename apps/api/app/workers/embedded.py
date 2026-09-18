"""Run the arq worker inside the API process, for single-service deploys.

docker-compose runs `worker` as its own container, which is the right shape
and the one production should grow back into. It is not deployable
everywhere: Render's free plan runs one service per blueprint entry, and a
Render Disk attaches to exactly one service, so a separate worker container
would have its own filesystem and could never read the bytes the API just
wrote to `settings.upload_dir` (see `app/rag/storage.py` on why those bytes
are local at all). Deploying the API alone is worse still -- `enqueue_ingest`
succeeds, nothing consumes the queue, and every document sits at `pending`
forever.

So this module puts the consumer in the same process as the producer, where
`upload_dir` is unambiguously the same disk. What it trades away, and why
that is a deliberate choice rather than an oversight:

* ingestion competes with request handling for one CPU and one database
  connection pool -- `settings.worker_max_jobs` is what bounds the second
  half of that, and it has to be set against `pool_size`/`max_overflow`
  knowing HTTP requests are drawing from the same pool now;
* the two cannot be scaled apart, so a queue backlog can only be answered
  by making the web service bigger;
* a restart (a deploy, or a free instance spinning down) cancels an
  in-flight job rather than draining it. arq re-queues it once the
  in-progress key expires, so it recovers on its own -- but the document
  reads `processing` for the ~10 minutes that takes.

Switching back to a dedicated worker is `RUN_EMBEDDED_WORKER=false` plus a
second service running `arq app.workers.settings.WorkerSettings`, once
`storage.py` is backed by object storage.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Protocol, cast

from arq.typing import WorkerSettingsType
from arq.worker import Worker, create_worker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.workers.settings import WorkerSettings

logger = get_logger(__name__)


class RunnableWorker(Protocol):
    """The one method `embedded_worker` needs, so tests can supply a worker
    that does not reach for Redis."""

    async def async_run(self) -> None: ...


def build_embedded_worker() -> Worker:
    """An arq worker configured exactly like the standalone one, minus the
    signal handling.

    `handle_signals=False` is the load-bearing argument. arq's `Worker`
    registers its own SIGINT/SIGTERM handlers in `__init__`, which inside
    this process would replace the ones uvicorn installed -- the platform's
    SIGTERM would then reach arq and never start uvicorn's graceful
    shutdown, cutting off in-flight HTTP requests instead of draining them.
    Shutdown is driven by the lifespan below instead.

    Must be called with the event loop already running: `Worker.__init__`
    captures `asyncio.get_event_loop()` and later schedules every job with
    `self.loop.create_task(...)`. Built at import time it would capture a
    loop uvicorn never runs.
    """
    # `WorkerSettingsType` is `type[WorkerSettingsBase]`, an arq Protocol
    # declaring every optional hook (`cron_jobs`, `on_shutdown`, ...).
    # `WorkerSettings` sets only the ones it uses, which is how arq's own
    # CLI is meant to be given it -- `get_kwargs` reads the class __dict__
    # and ignores the rest -- but leaves it structurally short of the
    # protocol for mypy.
    return create_worker(cast("WorkerSettingsType", WorkerSettings), handle_signals=False)


async def _run(worker: RunnableWorker) -> None:
    """Run the worker, and let it fail without failing the API with it.

    An unreachable Redis is the expected case here. Ingestion is broken
    either way, but a task that propagates would leave the app serving with
    a dead background task and nothing in the log to say so -- and the
    traceback would surface at shutdown, long after the fact.
    """
    try:
        await worker.async_run()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("embedded_worker_stopped", exc_info=exc)


@asynccontextmanager
async def embedded_worker(
    build: Callable[[], RunnableWorker] | None = None,
) -> AsyncIterator[None]:
    """Run the arq worker for the lifetime of the app, if configured to.

    A no-op unless `RUN_EMBEDDED_WORKER` is set, so the compose deployment
    -- which already runs `worker` as its own container -- does not end up
    draining one queue from two processes.
    """
    if not get_settings().run_embedded_worker:
        yield
        return

    # Resolved here rather than as a default argument: a default binds
    # `build_embedded_worker` at import time, so a test substituting the
    # factory on this module would be substituting a name nothing reads.
    worker = (build or build_embedded_worker)()
    task = asyncio.create_task(_run(worker))
    logger.info("embedded_worker_started")
    try:
        yield
    finally:
        # `async_run` never returns on its own, so cancellation is the only
        # way out; without it the lifespan's shutdown would hang until the
        # platform killed the container. `Worker.close()` is deliberately
        # not called: with `handle_signals=False` it re-enters arq's signal
        # path by name (`signal.SIGUSR1`, which does not exist on Windows)
        # and then gathers tasks this cancel already cancelled. The process
        # is exiting, and arq's health-check key carries its own TTL, so
        # there is nothing left behind that outlives it.
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("embedded_worker_stopped")
