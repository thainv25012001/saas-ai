"""Queueing a conversation for titling, and the policy for which get one."""

import uuid

from app.db.models import ConversationChannel
from app.workers.enqueue import enqueue
from app.workers.tasks import title_conversation_task

#: Which channels are worth paying to title.
#:
#: Only the playground for now, because only the playground lists
#: conversations. Titling every channel means buying a summary of every
#: customer conversation once the embedded widget ships -- real money for
#: labels nothing reads. Widening this is what a conversations inbox would do,
#: deliberately.
TITLED_CHANNELS = (ConversationChannel.PLAYGROUND,)


def should_title(channel: ConversationChannel) -> bool:
    return channel in TITLED_CHANNELS


async def enqueue_title(conversation_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    await enqueue(
        title_conversation_task,
        organization_id=str(organization_id),
        conversation_id=str(conversation_id),
    )
