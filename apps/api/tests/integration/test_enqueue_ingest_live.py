"""One live round-trip of `enqueue_ingest` against the real Redis this
suite already depends on -- not a mock.

`tests/unit/test_queue.py` monkeypatches `arq.create_pool`, so it proves
`enqueue_ingest` *would* call `pool.enqueue_job` with the right name and
kwargs, but it has never actually called `create_pool`,
`RedisSettings.from_dsn(settings.redis_url)`, or put a real job in a real
queue. That module's own docstring warns the consequence of a wrong job
name is silent: the job piles up in Redis with no worker listening, and
every test -- including that unit test -- stays green. This is Task 5's
first production caller of `enqueue_ingest`, and Task 5's review found a
real defect in the surrounding retry logic that a silently-failing enqueue
would make worse (a `pending` document stranded with no job and no way
back). This test is the one thing in the suite that actually exercises the
call end to end.

Not a "network call" in the sense the no-network-calls rule means: Redis is
already running on localhost and this whole suite already depends on it
being reachable (rate limiting, `_dispose_shared_engine`'s
`get_redis().aclose()`). This adds no new external dependency, only a real
exercise of one the suite already requires.
"""

import uuid

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.constants import job_key_prefix

from app.core.config import get_settings
from app.rag.queue import enqueue_ingest
from app.workers.tasks import ingest_document_task

pytestmark = pytest.mark.anyio


async def test_enqueue_ingest_lands_a_real_job_in_redis_under_the_registered_name() -> None:
    document_id, organization_id = uuid.uuid4(), uuid.uuid4()

    await enqueue_ingest(document_id, organization_id)

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        queued = await pool.queued_jobs()
        matches = [
            job
            for job in queued
            if job.function == ingest_document_task.__name__
            and job.kwargs.get("document_id") == str(document_id)
        ]
        assert matches, (
            "enqueue_ingest did not put a real job in Redis under "
            f"{ingest_document_task.__name__!r} -- got: {[j.function for j in queued]}"
        )
        job = matches[0]
        assert job.kwargs == {
            "organization_id": str(organization_id),
            "document_id": str(document_id),
        }
    finally:
        # Remove the job this test just created so it doesn't sit in Redis
        # forever, or get picked up by a real worker process pointed at
        # the same Redis instance and fail loudly against a document id
        # that was never created.
        for job in await pool.queued_jobs():
            if job.kwargs.get("document_id") == str(document_id) and job.job_id is not None:
                await pool.zrem(pool.default_queue_name, job.job_id)
                await pool.delete(job_key_prefix + job.job_id)
        await pool.aclose()
