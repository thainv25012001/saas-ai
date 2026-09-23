"""`enqueue_evaluation_run` enqueues by the function's own name, and
`run_evaluation_task` is registered through `arq.worker.func` (for its own
timeout) -- so the two must still agree on the string, or runs sit
`pending` forever with nothing saying why. Redis is never touched here
(`create_pool` is monkeypatched).
"""

import uuid

import pytest
from arq.worker import Function

from app.evaluations.queue import enqueue_evaluation_run
from app.workers import enqueue as enqueue_module
from app.workers.settings import WorkerSettings
from tests.unit._queue_stubs import FakePool

pytestmark = pytest.mark.anyio


@pytest.fixture
def pool(monkeypatch) -> FakePool:
    fake = FakePool()

    async def _fake_create_pool(_redis_settings: object) -> FakePool:
        return fake

    monkeypatch.setattr(enqueue_module, "create_pool", _fake_create_pool)
    return fake


async def test_the_enqueued_job_is_one_the_worker_handles_with_the_run_arguments(pool):
    run_id, organization_id = uuid.uuid4(), uuid.uuid4()

    await enqueue_evaluation_run(run_id, organization_id)

    registered = {fn.name for fn in WorkerSettings.functions if isinstance(fn, Function)}
    assert pool.enqueued["name"] == "run_evaluation_task"
    assert pool.enqueued["name"] in registered
    assert pool.enqueued["kwargs"] == {
        "organization_id": str(organization_id),
        "evaluation_run_id": str(run_id),
    }
