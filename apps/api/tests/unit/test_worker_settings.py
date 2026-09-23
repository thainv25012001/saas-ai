"""An arq worker started with an empty `functions` list boots happily and
then silently processes nothing -- no error, just a queue nobody drains.
These tests pin the one thing that turns that into a loud, checkable fact:
the ingest job is actually registered, pointed at this app's Redis, and
configured to retry and eventually time out rather than hang forever.
"""

from arq.connections import RedisSettings

from app.workers.settings import WorkerSettings
from app.workers.tasks import ingest_document_task


def test_ingest_document_task_is_registered() -> None:
    assert ingest_document_task in WorkerSettings.functions


def test_functions_list_is_not_empty() -> None:
    # Belt-and-braces on top of the identity check above: an empty list
    # would still make the first assertion's absence-of-match meaningless
    # if `functions` were, say, accidentally reassigned to `[]` elsewhere.
    assert len(WorkerSettings.functions) >= 1


def test_redis_settings_is_derived_from_settings_redis_url() -> None:
    from app.core.config import get_settings

    expected = RedisSettings.from_dsn(get_settings().redis_url)
    actual = WorkerSettings.redis_settings
    assert isinstance(actual, RedisSettings)
    assert (actual.host, actual.port, actual.database) == (
        expected.host,
        expected.port,
        expected.database,
    )


def test_retries_and_job_timeout_are_configured() -> None:
    assert WorkerSettings.max_tries == 3
    assert WorkerSettings.job_timeout == 600


def test_max_jobs_is_bounded_so_every_concurrent_job_can_hold_two_connections() -> None:
    """`ingest_document` holds up to two of this process's own pool
    connections per job at once (the caller's `session` plus a second,
    independent `tenant_session`). Left at arq's default of 10, saturating
    the worker could want 20 connections against `app/db/session.py`'s
    15-connection pool (`pool_size=10 + max_overflow=5`) -- not a deadlock,
    but a `pool_timeout` stall on every job past the first few. This pins
    `max_jobs` actually being set low enough for that arithmetic to hold,
    not just present."""
    from app.core.config import get_settings

    assert WorkerSettings.max_jobs == get_settings().worker_max_jobs
    pool_capacity = 10 + 5  # app/db/session.py's pool_size + max_overflow
    assert WorkerSettings.max_jobs * 2 <= pool_capacity


def test_on_startup_configures_logging_in_the_worker_process() -> None:
    """`configure_logging` is called from `app/main.py`, which the worker
    process never imports. Without an `on_startup` hook the worker ran on
    structlog's defaults: `LOG_LEVEL` ignored, and output in a different
    shape from every other process in the deployment.
    """
    import asyncio

    import structlog

    from app.core.config import get_settings

    assert callable(WorkerSettings.on_startup)

    structlog.reset_defaults()
    asyncio.run(WorkerSettings.on_startup({}))

    # `configure_logging` installs a JSON renderer and a level filter built
    # from `settings.log_level`; structlog's defaults have neither.
    config = structlog.get_config()
    assert any(
        isinstance(processor, structlog.processors.JSONRenderer)
        for processor in config["processors"]
    )
    expected_level = structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), get_settings().log_level.upper())
    )
    assert config["wrapper_class"] is expected_level


def test_title_conversation_task_is_registered() -> None:
    """Same reasoning as the ingest job above: an unregistered function is
    not an error, it is a queue nobody drains -- conversations would simply
    never get a title, with nothing anywhere saying why."""
    from app.workers.tasks import title_conversation_task

    assert title_conversation_task in WorkerSettings.functions


def test_import_products_task_is_registered() -> None:
    """Task 3's own version of the same risk this whole file exists to
    catch: Phase 4 shipped an entire reviewed feature inert because its job
    was never added to `functions`. An import that uploads cleanly and
    queues a job that nothing ever drains would look identical to success
    from the client's side, forever."""
    from app.workers.tasks import import_products_task

    assert import_products_task in WorkerSettings.functions


def test_run_evaluation_task_is_registered_under_its_own_name_with_a_one_hour_timeout() -> None:
    """Registered through `arq.worker.func`, not bare, because an evaluation
    run needs its own 1-hour timeout (docs/PHASE-6.md §5) rather than the
    worker-wide `job_timeout` -- so `run_evaluation_task in functions` (the
    identity check the jobs above use) would be False here by construction.
    What actually has to hold is what arq dispatches on: a `Function` whose
    name is exactly the string `enqueue` sends (`task.__name__`), wrapping
    this very coroutine."""
    from arq.worker import Function

    from app.workers.tasks import run_evaluation_task

    registered = [
        entry
        for entry in WorkerSettings.functions
        if isinstance(entry, Function) and entry.name == run_evaluation_task.__name__
    ]
    assert len(registered) == 1
    [function] = registered
    assert function.name == "run_evaluation_task"
    assert function.coroutine is run_evaluation_task
    assert function.timeout_s == 3600


def test_run_evaluation_task_pins_the_max_tries_the_runner_is_told() -> None:
    """`run_evaluation` must know which try is the last one (a time-budget
    hand-off there fails the run instead of raising `Retry`, which arq would
    never act on) -- so the `max_tries` arq enforces and the one the task
    passes down must be the same number."""
    from arq.worker import Function

    from app.workers.tasks import EVALUATION_MAX_TRIES, run_evaluation_task

    [function] = [
        entry
        for entry in WorkerSettings.functions
        if isinstance(entry, Function) and entry.coroutine is run_evaluation_task
    ]
    assert function.max_tries == EVALUATION_MAX_TRIES
    assert function.timeout_s is not None
    from app.evaluations.runner import EVALUATION_TIME_BUDGET_S

    assert EVALUATION_TIME_BUDGET_S < function.timeout_s
