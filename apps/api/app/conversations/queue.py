"""Enqueue a titling job onto arq's Redis queue.

A thin wrapper for the same reason `app/rag/queue.py` is one: the caller
depends on a single function instead of arq's pool API, this app's Redis DSN
shape, and the job name string all at once.
"""

import uuid

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.db.models import ConversationChannel
from app.workers.tasks import title_conversation_task

#: Which channels are worth paying to title.
#:
#: Only the playground for now, because only the playground lists
#: conversations. Titling every channel means buying a summary of every
#: customer conversation once the embedded widget ships -- real money for
#: labels nothing reads. Widening this is what a conversations inbox would
#: do, deliberately.
TITLED_CHANNELS = (ConversationChannel.PLAYGROUND,)


def should_title(channel: ConversationChannel) -> bool:
    return channel in TITLED_CHANNELS


async def enqueue_title(conversation_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `title_conversation_task` for this conversation.

    Opens and closes its own pool per call, like `enqueue_ingest`: this runs
    once per conversation, not once per turn, so pool reuse would add a
    lifecycle to manage for no measurable benefit.
    """
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(
            # The function object's own `__name__`, never a literal: arq
            # derives a job's registered name from it (see
            # `WorkerSettings.functions`), and a copy here would drift the
            # moment the function is renamed. The failure is invisible --
            # jobs pile up in Redis with no worker listening, and
            # conversations simply never get titles.
            title_conversation_task.__name__,
            organization_id=str(organization_id),
            conversation_id=str(conversation_id),
        )
    finally:
        # `aclose()`, not the deprecated `close()` -- see the note on the
        # same line in `app/rag/queue.py`: redis-py emits a
        # `DeprecationWarning` from `close()`, and this suite turns warnings
        # into hard failures.
        await pool.aclose()
