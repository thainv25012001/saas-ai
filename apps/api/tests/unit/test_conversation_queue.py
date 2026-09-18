"""`enqueue_title` names its job with a bare string against a function in
another module, and fails the same silent way if the two drift: jobs pile up
in Redis unclaimed, conversations simply never get titles, and nothing says
why. Redis is never touched here (`create_pool` is monkeypatched).
"""

import uuid

import pytest

from app.conversations.queue import enqueue_title, should_title
from app.db.models import ConversationChannel
from app.workers import enqueue as enqueue_module
from app.workers.settings import WorkerSettings
from app.workers.tasks import title_conversation_task
from tests.unit._queue_stubs import FakePool

pytestmark = pytest.mark.anyio


@pytest.fixture
def pool(monkeypatch) -> FakePool:
    fake = FakePool()

    async def _fake_create_pool(_redis_settings: object) -> FakePool:
        return fake

    monkeypatch.setattr(enqueue_module, "create_pool", _fake_create_pool)
    return fake


async def test_enqueue_title_uses_the_registered_task_name(pool):
    conversation_id, organization_id = uuid.uuid4(), uuid.uuid4()

    await enqueue_title(conversation_id, organization_id)

    assert pool.enqueued["name"] == title_conversation_task.__name__
    assert pool.enqueued["kwargs"] == {
        "organization_id": str(organization_id),
        "conversation_id": str(conversation_id),
    }


async def test_the_enqueued_name_is_one_the_worker_actually_handles(pool):
    await enqueue_title(uuid.uuid4(), uuid.uuid4())

    assert pool.enqueued["name"] in {fn.__name__ for fn in WorkerSettings.functions}


async def test_the_pool_is_closed(pool):
    await enqueue_title(uuid.uuid4(), uuid.uuid4())

    assert pool.closed


def test_only_playground_conversations_are_titled():
    """The cost guard. Titling every channel buys a summary of every customer
    conversation once the embedded widget ships."""
    assert should_title(ConversationChannel.PLAYGROUND)
    assert not should_title(ConversationChannel.WIDGET)
    assert not should_title(ConversationChannel.API)
