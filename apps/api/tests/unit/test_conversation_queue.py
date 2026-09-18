"""`enqueue_title` names its job with a bare string against a function in
another module, exactly like `enqueue_ingest` -- and fails the same silent
way if the two drift: jobs pile up in Redis unclaimed, conversations simply
never get titles, and nothing says why. Redis is never touched here
(`create_pool` is monkeypatched).
"""

import uuid
from typing import Any

import pytest

from app.conversations import queue as queue_module
from app.workers.settings import WorkerSettings
from app.workers.tasks import title_conversation_task

pytestmark = pytest.mark.anyio


class _FakePool:
    def __init__(self) -> None:
        self.enqueued: dict[str, Any] = {}
        self.closed = False

    async def enqueue_job(self, name: str, **kwargs: Any) -> None:
        self.enqueued = {"name": name, "kwargs": kwargs}

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def pool(monkeypatch) -> _FakePool:
    fake = _FakePool()

    async def _fake_create_pool(_redis_settings: object) -> _FakePool:
        return fake

    monkeypatch.setattr(queue_module, "create_pool", _fake_create_pool)
    return fake


async def test_enqueue_title_uses_the_registered_task_name(pool):
    conversation_id, organization_id = uuid.uuid4(), uuid.uuid4()

    await queue_module.enqueue_title(conversation_id, organization_id)

    assert pool.enqueued["name"] == title_conversation_task.__name__
    assert pool.enqueued["kwargs"] == {
        "organization_id": str(organization_id),
        "conversation_id": str(conversation_id),
    }


async def test_the_enqueued_name_is_one_the_worker_actually_handles(pool):
    await queue_module.enqueue_title(uuid.uuid4(), uuid.uuid4())

    assert pool.enqueued["name"] in {fn.__name__ for fn in WorkerSettings.functions}


async def test_the_pool_is_closed(pool):
    await queue_module.enqueue_title(uuid.uuid4(), uuid.uuid4())

    assert pool.closed
