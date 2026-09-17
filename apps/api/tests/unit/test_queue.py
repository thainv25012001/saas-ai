"""`enqueue_ingest` couples to `"ingest_document_task"` as a bare string,
against a function defined in another module entirely
(`app/workers/tasks.py`). Nothing links the two: renaming the function
without updating this string would enqueue jobs arq's worker has no handler
for, and the failure is silent -- they simply pile up in Redis, unclaimed,
with nothing in a log or a test to say why. This test is what makes that
drift loud instead: it never touches Redis (`create_pool` is monkeypatched),
so it costs nothing to run on every change.
"""

import uuid
from typing import Any

import pytest

from app.rag import queue as queue_module
from app.workers.settings import WorkerSettings
from app.workers.tasks import ingest_document_task

pytestmark = pytest.mark.anyio


class _FakePool:
    def __init__(self) -> None:
        self.enqueued: dict[str, Any] = {}
        self.closed = False

    async def enqueue_job(self, name: str, **kwargs: Any) -> None:
        self.enqueued = {"name": name, "kwargs": kwargs}

    async def aclose(self) -> None:
        self.closed = True


async def test_enqueue_ingest_uses_the_registered_task_name(monkeypatch):
    pool = _FakePool()

    async def _fake_create_pool(_redis_settings: object) -> _FakePool:
        return pool

    monkeypatch.setattr(queue_module, "create_pool", _fake_create_pool)

    document_id, organization_id = uuid.uuid4(), uuid.uuid4()
    await queue_module.enqueue_ingest(document_id, organization_id)

    # The string this call enqueues under must match the *actual* function
    # name -- not a string that merely happens to equal it today.
    assert pool.enqueued["name"] == ingest_document_task.__name__
    assert pool.enqueued["kwargs"] == {
        "organization_id": str(organization_id),
        "document_id": str(document_id),
    }
    assert pool.closed is True

    # And the worker must actually be listening for a job under that same
    # name -- WorkerSettings.functions registers by identity, so arq
    # derives the job name from this same `__name__`.
    assert ingest_document_task in WorkerSettings.functions
