"""Running the arq worker inside the API process.

Render's free plan runs one service, and a Render Disk attaches to exactly
one service, so the split `api` + `worker` shape docker-compose uses cannot
be deployed there: a separate worker container would have its own
filesystem and could never read the bytes the API just wrote to
`settings.upload_dir`. With no worker at all, `enqueue_ingest` succeeds,
the job sits in Redis forever and every document stays `pending` -- which
is exactly the production symptom this module exists to fix.

These tests pin the two things that make an in-process worker safe to put
next to uvicorn: it must not take over the process's signal handlers, and
it must be built on the loop that is already running.
"""

import asyncio
from collections.abc import Callable, Iterator

import pytest

from app.core.config import get_settings
from app.workers.embedded import build_embedded_worker, embedded_worker
from app.workers.tasks import ingest_document_task

pytestmark = pytest.mark.anyio


@pytest.fixture
def set_embedded_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    """Set `RUN_EMBEDDED_WORKER` and drop the `get_settings` cache around it.

    `get_settings` is `lru_cache`d, so `monkeypatch.setenv` on its own
    changes nothing the code under test can see -- and a cache left
    populated afterwards would leak this setting into every later test in
    the session.
    """

    def _set(enabled: bool) -> None:
        monkeypatch.setenv("RUN_EMBEDDED_WORKER", "true" if enabled else "false")
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


class FakeWorker:
    """Stands in for `arq.worker.Worker` so these tests need no Redis.

    `async_run` blocks forever, like the real one does: what matters here
    is what `embedded_worker` does with a worker that never finishes on its
    own.
    """

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def async_run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped = True
            raise


def test_the_embedded_worker_is_off_unless_asked_for() -> None:
    """docker-compose runs `worker` as its own container. Defaulting this on
    would put a second consumer inside `api` as well, draining one Redis
    queue from two processes that were never sized against the same
    connection pool together."""
    assert get_settings().run_embedded_worker is False


async def test_the_embedded_worker_runs_the_ingest_job() -> None:
    """The same registration `WorkerSettings` gives the standalone worker --
    an arq worker with an empty `functions` list boots fine and then drains
    nothing, which looks identical to the bug being fixed here."""
    worker = build_embedded_worker()
    assert ingest_document_task.__name__ in worker.functions


async def test_the_embedded_worker_leaves_the_process_signal_handlers_alone() -> None:
    """arq's `Worker` installs its own SIGINT/SIGTERM handlers by default,
    at construction time. Inside the API process those replace uvicorn's, so
    Render's SIGTERM would reach arq and never start uvicorn's graceful
    shutdown: in-flight HTTP requests get cut off instead of drained."""
    worker = build_embedded_worker()
    assert worker._handle_signals is False


async def test_the_embedded_worker_binds_to_the_already_running_loop() -> None:
    """`Worker.__init__` captures `asyncio.get_event_loop()` and later calls
    `self.loop.create_task(...)`. Built at import time -- before uvicorn
    starts its loop -- it would capture a different loop and every job would
    be scheduled onto one nothing is running."""
    worker = build_embedded_worker()
    assert worker.loop is asyncio.get_running_loop()


async def test_no_worker_is_started_when_the_setting_is_off(
    set_embedded_worker: Callable[[bool], None],
) -> None:
    set_embedded_worker(False)
    built: list[FakeWorker] = []

    def build() -> FakeWorker:
        worker = FakeWorker()
        built.append(worker)
        return worker

    async with embedded_worker(build=build):
        await asyncio.sleep(0)

    assert built == []


async def test_the_worker_runs_for_as_long_as_the_app_does(
    set_embedded_worker: Callable[[bool], None],
) -> None:
    set_embedded_worker(True)
    worker = FakeWorker()

    async with embedded_worker(build=lambda: worker):
        # One loop turn is all the task needs to reach its first await.
        await asyncio.sleep(0)
        assert worker.started is True
        assert worker.stopped is False


async def test_the_worker_is_stopped_when_the_app_shuts_down(
    set_embedded_worker: Callable[[bool], None],
) -> None:
    """`async_run` never returns on its own. Left running, the lifespan's
    shutdown would hang and the platform would kill the container rather
    than it exiting."""
    set_embedded_worker(True)
    worker = FakeWorker()

    # The timeout is the assertion for the "never cancelled" case: without
    # it, a shutdown that fails to stop the worker hangs this test (and CI)
    # forever instead of failing.
    async with asyncio.timeout(5):
        async with embedded_worker(build=lambda: worker):
            await asyncio.sleep(0)

    assert worker.stopped is True


async def test_a_failing_worker_does_not_take_the_api_down_with_it(
    set_embedded_worker: Callable[[bool], None],
) -> None:
    """Redis being unreachable must not stop the app serving HTTP. Ingestion
    is degraded either way; answering `/health` and the read endpoints is
    strictly better than the container crash-looping -- and on Render a
    failing health check takes the whole deploy down."""
    set_embedded_worker(True)

    class ExplodingWorker:
        async def async_run(self) -> None:
            raise ConnectionError("redis is down")

    async with embedded_worker(build=ExplodingWorker):
        await asyncio.sleep(0)


async def test_the_api_runs_the_embedded_worker_for_its_own_lifespan(
    set_embedded_worker: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not the worker: nothing above starts a worker unless
    `create_app` actually hangs `embedded_worker` off the app's lifespan.
    Only the factory is substituted, so the setting, the context manager and
    the app's own startup/shutdown are the real ones."""
    set_embedded_worker(True)
    worker = FakeWorker()
    monkeypatch.setattr("app.workers.embedded.build_embedded_worker", lambda: worker)

    from app.main import create_app

    app = create_app()

    async with asyncio.timeout(5):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert worker.started is True

    assert worker.stopped is True
