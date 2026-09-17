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
