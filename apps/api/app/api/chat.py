"""`POST /api/v1/chat/stream` -- see `docs/PHASE-2.md` §4 for the event
envelope and the reasoning behind the response headers.

The one rule this module exists to enforce: an error raised *before* the
first byte of the stream is decided (unknown agent, cross-tenant agent,
provider misconfiguration, bad conversation id) must come back as a normal
JSON error envelope with the right HTTP status -- never as a 200 whose body
happens to contain an `error` event. Once `StreamingResponse` sends its
`http.response.start` ASGI message the status line is committed, so that
distinction has to be enforced by pulling the *first* event out of
`ChatService.send()` before a `StreamingResponse` is ever constructed. Errors
after that point are already inside the committed 200 and are delivered as an
in-band `error` event -- which is exactly what `ChatService.send()` already
yields for a mid-stream provider failure (see `app/chat/service.py`).
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_tenant
from app.chat.service import (
    ChatError,
    ChatEvent,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
)
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel
from app.llm.base import LLMProvider

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# A module constant (not a literal inline) so a test can `monkeypatch` it down
# to a few milliseconds instead of waiting out the real 15s interval -- no
# test in this suite may sleep for the real value.
HEARTBEAT_INTERVAL_SECONDS = 15.0

# `_pump` is the only producer, so this is free backpressure: if the consumer
# ever stalls, `_pump`'s `await queue.put(...)` blocks instead of buffering
# an unbounded number of already-generated events in memory.
_QUEUE_MAXSIZE = 32

_PING = b": ping\n\n"

# Deliberately the exact string `app/graphql/schema.py`'s `AppErrorExtension`
# uses for the same situation (an unrecognised exception reaching a client
# surface), so REST and GraphQL agree on what an unexplained failure looks
# like. Never interpolate the real exception into this -- see the
# `isinstance(item, BaseException)` branch below for why.
_INTERNAL_ERROR_MESSAGE = "internal server error"


class _StreamDone:
    """Sentinel `_pump` puts on the queue once `events` is exhausted.

    A dedicated type (rather than `None` or an `object()` singleton) so
    `isinstance` checks read unambiguously below.
    """

    __slots__ = ()


_STREAM_DONE = _StreamDone()

_QueueItem = ChatEvent | BaseException | _StreamDone
_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]


class ChatStreamRequest(BaseModel):
    agent_id: uuid.UUID
    message: str
    conversation_id: uuid.UUID | None = None


def get_chat_provider() -> LLMProvider | None:
    """Production default: `None` lets `ChatService` resolve the agent's own
    configured provider. Tests override this FastAPI dependency to inject a
    `FakeProvider`, which is what keeps every test in this module off the
    network."""
    return None


def _event_payload(event: ChatEvent) -> dict[str, object]:
    if isinstance(event, ChatMessageStart):
        return {
            "type": "message_start",
            "conversation_id": str(event.conversation_id),
            "message_id": str(event.message_id),
        }
    if isinstance(event, ChatTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ChatMessageEnd):
        return {
            "type": "message_end",
            "usage": event.usage.model_dump(),
            "cost_usd": str(event.cost_usd) if event.cost_usd is not None else None,
            "latency_ms": event.latency_ms,
            "model": event.model,
            "prompt_version_id": (
                str(event.prompt_version_id) if event.prompt_version_id is not None else None
            ),
        }
    if isinstance(event, ChatError):
        return {"type": "error", "code": event.code, "message": event.message}
    raise AssertionError(f"unhandled ChatEvent variant: {event!r}")  # pragma: no cover


def _sse(data: dict[str, object]) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


async def _pump(events: AsyncIterator[ChatEvent], queue: "asyncio.Queue[_QueueItem]") -> None:
    """Drains `events` into `queue` at its own pace, on a task the heartbeat
    loop's timeouts never touch.

    This runs as a separate task specifically so that a heartbeat timeout
    never has to cancel `events.__anext__()` itself. `events` is
    `ChatService.send()`'s async generator, and at the moment a timeout would
    fire it is typically suspended mid-await inside the LLM provider's own
    stream (e.g. waiting on a socket, or -- in tests -- `asyncio.sleep`).
    Wrapping that await directly in `asyncio.wait_for` and letting it cancel
    on timeout tears down that await with a `CancelledError`, which for an
    `asyncpg` connection reachable at that point (this generator also drives
    DB writes) leaves the connection unusable for the rest of the request --
    the final commit then fails with `PendingRollbackError`. Consuming
    through a queue instead means `asyncio.wait_for` only ever cancels
    `queue.get()`, which is always safe to cancel and retry.
    """
    try:
        async for event in events:
            await queue.put(event)
    except Exception as exc:  # noqa: BLE001 - forwarded to the consumer below, not swallowed
        await queue.put(exc)
        return
    await queue.put(_STREAM_DONE)


async def _stream_body(
    events: AsyncIterator[ChatEvent],
    first_event: ChatEvent,
    session_cm: AbstractAsyncContextManager[AsyncSession],
) -> AsyncIterator[bytes]:
    """The actual SSE body, run only after `first_event` has already been
    pulled successfully -- so everything yielded here happens after the 200
    status line is committed, and any failure past this point has nowhere to
    go but an in-band `error` event (already the shape `ChatService.send()`
    yields for a mid-stream provider failure).

    A heartbeat comment is emitted whenever `HEARTBEAT_INTERVAL_SECONDS`
    elapses without a new event, so idle connections are not reaped by a
    proxy while the model is still "thinking".
    """
    queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    pump_task = asyncio.create_task(_pump(events, queue))
    exc_info: _ExcInfo = (None, None, None)
    # Set the moment a `ChatMessageEnd` is actually dequeued -- i.e. the LLM
    # call finished, and `ChatService.send()` already flushed the assistant
    # message and the `usage_events` row for it (see Finding 5: a client
    # that then disconnects before this generator reaches `_STREAM_DONE`
    # rolls that flush back via the `except BaseException` branch below,
    # discarding usage for a call the organization has already been billed
    # for by the provider -- worth a loud log line even though fixing it for
    # real needs usage accounting outside this transaction; see
    # `docs/PHASE-2.md` §5).
    completed_message_end: ChatMessageEnd | None = None
    try:
        yield _sse(_event_payload(first_event))
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                yield _PING
                continue
            if isinstance(item, _StreamDone):
                break
            if isinstance(item, BaseException):
                # Every provider failure `ChatService.send()` itself expects
                # is already normalized to `AppError` and caught *inside*
                # that generator, which turns it into a yielded `ChatError`
                # -- see the `except AppError` block in `app/chat/service.py`.
                # An exception reaching this far is therefore not one of
                # those: a bug, or an `AppError` subclass raised from a code
                # path that generator does not wrap (e.g. persistence, after
                # the streaming loop) -- frequently a database error itself.
                #
                # Three things must all happen here, matching how
                # `app/graphql/schema.py`'s `AppErrorExtension` treats the
                # same situation:
                #  1. The client gets a fixed, generic message. `str(item)`
                #     of a raw `IntegrityError` contains the failing SQL,
                #     bound parameters (including the user's own message
                #     text), and constraint/table names -- exactly the leak
                #     `AppErrorExtension`'s own comment names.
                #  2. The real exception, with its traceback, goes to the
                #     log -- the one place someone who can act on it will
                #     see it. Without this, a bug here produces no
                #     traceback anywhere and the access log still reads
                #     `status=200`, since the status line genuinely was 200.
                #  3. `exc_info` is set so the `finally` below rolls the
                #     transaction back instead of committing it. Unlike a
                #     normalized `AppError` (where partial assistant text is
                #     deliberately kept because the user already saw those
                #     tokens on screen), an exception this generator did not
                #     expect is often itself evidence the data is suspect --
                #     committing a transaction whose failure is not
                #     understood risks persisting inconsistent state.
                logger.error("chat_stream_unexpected_error", exc_info=item)
                exc_info = (type(item), item, item.__traceback__)
                error_payload = _event_payload(
                    ChatError(code="internal_error", message=_INTERNAL_ERROR_MESSAGE)
                )
                yield _sse(error_payload)
                break
            if isinstance(item, ChatMessageEnd):
                completed_message_end = item
            yield _sse(_event_payload(item))
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        if completed_message_end is not None:
            logger.warning(
                "chat_stream_discarded_completed_turn_usage",
                model=completed_message_end.model,
                input_tokens=completed_message_end.usage.input_tokens,
                output_tokens=completed_message_end.usage.output_tokens,
                cost_usd=(
                    str(completed_message_end.cost_usd)
                    if completed_message_end.cost_usd is not None
                    else None
                ),
            )
        raise
    finally:
        try:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
        finally:
            # A nested `finally`, not a sibling statement: `await pump_task`
            # above only suppresses `CancelledError` (`_pump` itself only
            # ever raises that or nothing). Any other `BaseException`
            # surfacing from it must still not skip closing the session --
            # that would leak the exact connection the pre-stream-error path
            # is guarded against leaking (see the leak test on the early
            # `events.__anext__()` failure below).
            await session_cm.__aexit__(*exc_info)


@router.post("/stream")
async def chat_stream(
    payload: ChatStreamRequest,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    provider: Annotated[LLMProvider | None, Depends(get_chat_provider)],
) -> StreamingResponse:
    # Opened manually (not via `async with`) because the session has to
    # outlive this function: it is read from and written to for as long as
    # the SSE body below keeps streaming, well after this coroutine returns
    # its `StreamingResponse`. `_stream_body`'s `finally` closes it once the
    # generator is exhausted or cancelled.
    session_cm = tenant_session(tenant)
    session = await session_cm.__aenter__()
    service = ChatService(session, tenant, provider_override=provider)
    events = service.send(
        payload.agent_id,
        payload.message,
        conversation_id=payload.conversation_id,
        channel=ConversationChannel.PLAYGROUND,
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

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _stream_body(events, first_event, session_cm),
        media_type="text/event-stream",
        headers=headers,
    )
