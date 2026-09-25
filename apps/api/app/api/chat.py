"""`POST /api/v1/chat/stream` -- the dashboard/playground chat route.

The streaming body itself (`stream_body`, `_pump`, `_queue_title`, the event
payload shape, the SSE headers) moved to `app/api/streaming.py` in Phase 8
Task 2 so the widget's public chat route (`app/api/widget.py`, Task 3) can
share it. This module keeps only what is specific to the authenticated,
per-user route: the request schema, the fake-provider test seam, the rate
limit, and the route itself -- including the first-event pre-read pattern
that keeps a pre-stream failure a plain JSON error rather than an in-band
`error` event (see `app/api/streaming.py`'s module docstring for why that
distinction matters).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.schemas import Provider
from app.api.streaming import SSE_HEADERS, stream_body
from app.auth.dependencies import get_current_tenant
from app.chat.service import ChatMessageStart, ChatService
from app.conversations.queue import should_title
from app.core.rate_limit import enforce_rate_limit
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel
from app.llm.base import LLMProvider

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


# This is the only endpoint in the application that spends money, so both
# the size of a single request and how many of them one user may make are
# bounded here.
#
# 8_000 characters is roughly 2_000 tokens -- far more than anyone types into
# a chat box, comfortably inside every model's input window alongside the
# system prompt and 20 turns of history, and small enough that the `messages`
# row it becomes stays bounded. A larger body is a client bug or an abuse
# attempt, not a real question, and rejecting it before the provider call is
# the difference between a 422 and a paid request.
MAX_MESSAGE_LENGTH = 8_000

# 30 messages per minute per user. A person having a fast back-and-forth
# conversation sends at most a handful a minute, so this is generous for
# every real interaction while capping a runaway client (a retry loop, a
# script) at a spend the organization can survive. Keyed per user rather
# than per IP: unlike `register`/`login` this route is authenticated, so the
# identity is known and one office behind a single NAT address must not
# throttle itself.
CHAT_RATE_LIMIT = 30
CHAT_RATE_LIMIT_WINDOW_SECONDS = 60


class ChatStreamRequest(BaseModel):
    agent_id: uuid.UUID
    # `min_length=1`: an empty string is not a question, and without this it
    # creates a full billable turn plus a persisted empty `messages` row.
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    conversation_id: uuid.UUID | None = None
    # Answer this one turn with a different provider/model than the agent is
    # configured for, changing nothing about the agent itself -- what the
    # playground's model picker sends. `None` means "use the agent's own",
    # which is what every other caller sends and what keeps an untouched
    # playground byte-identical to before this existed.
    #
    # `Provider` is the same annotated type the agent create/update schemas
    # use, so an unrecognised provider name is rejected here the way it is
    # there: one wording of the rule, and FastAPI's `RequestValidationError`
    # handler turns it into a 422 JSON envelope before the route body runs --
    # long before a `StreamingResponse` could commit a 200.
    provider: Provider = None
    # Bounded exactly like `CreateAgentInput.model`. `min_length=1` is the
    # load-bearing half: `""` is not "no override", and without a floor it
    # reaches the vendor as a blank model id and buys a 400. The model is
    # deliberately *not* checked against `app.llm.catalog` -- OpenRouter's
    # catalog is a live HTTP fetch, and validating it here would put a network
    # round-trip in front of every chat request. An unknown model fails at the
    # provider and surfaces as a normal mid-stream error event, exactly as a
    # bad model stored on an agent does today.
    model: str | None = Field(default=None, min_length=1, max_length=100)


def get_chat_provider() -> LLMProvider | None:
    """Production default: `None` lets `ChatService` resolve the agent's own
    configured provider. Tests override this FastAPI dependency to inject a
    `FakeProvider`, which is what keeps every test in this module off the
    network."""
    return None


@router.post("/stream")
async def chat_stream(
    payload: ChatStreamRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    provider: Annotated[LLMProvider | None, Depends(get_chat_provider)],
) -> StreamingResponse:
    # Before anything is opened or written: the limiter itself fails open on
    # a Redis outage (see `enforce_rate_limit`), so this cannot turn a cache
    # blip into an unusable playground. `user_id` is always set for a token
    # issued by `AuthService`; the organization is the fallback so a future
    # tenant context without a user (e.g. the Phase 7 widget) is still
    # bounded by something rather than sharing one global key.
    rate_limit_subject = tenant.user_id or tenant.organization_id
    await enforce_rate_limit(
        f"chat:{rate_limit_subject}",
        limit=CHAT_RATE_LIMIT,
        window_seconds=CHAT_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Opened manually (not via `async with`) because the session has to
    # outlive this function: it is read from and written to for as long as
    # the SSE body below keeps streaming, well after this coroutine returns
    # its `StreamingResponse`. `stream_body`'s `finally` closes it once the
    # generator is exhausted or cancelled.
    session_cm = tenant_session(tenant)
    session = await session_cm.__aenter__()
    service = ChatService(session, tenant, provider_override=provider)
    events = service.send(
        payload.agent_id,
        payload.message,
        conversation_id=payload.conversation_id,
        channel=ConversationChannel.PLAYGROUND,
        override_provider=payload.provider,
        override_model=payload.model,
    )

    # Pulling the first event runs `ChatService.send()` up to (and including)
    # its first `yield ChatMessageStart(...)`. Every check that must produce
    # a plain JSON error response -- unknown/cross-tenant agent, an
    # unresolvable provider, a bad conversation id -- happens in that same
    # span, so a raised `AppError` here propagates straight out of this
    # route coroutine to `app.main`'s `AppError` handler, before any
    # `StreamingResponse` (and its committed 200 status line) exists.
    try:
        first_event = await events.__anext__()
    except BaseException as exc:
        await session_cm.__aexit__(type(exc), exc, exc.__traceback__)
        raise

    # Decided here, where the channel is a literal in this function rather than
    # something round-tripped through the service and back. `should_title` is
    # the policy -- which channels are worth paying to title -- and this route
    # is the only caller that knows which channel it is.
    title_conversation_id = (
        first_event.conversation_id
        if isinstance(first_event, ChatMessageStart)
        and first_event.created
        and should_title(ConversationChannel.PLAYGROUND)
        else None
    )

    return StreamingResponse(
        stream_body(events, first_event, session_cm, title_conversation_id, tenant.organization_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
