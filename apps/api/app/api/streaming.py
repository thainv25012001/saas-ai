"""The SSE streaming body shared by every chat surface -- see
`docs/PHASE-2.md` §4 for the event envelope and the reasoning behind the
response headers.

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

Lifted out of `app/api/chat.py` (Phase 8 Task 2) so the widget's public chat
route can share it: `_event_payload` is now the public `event_payload`, and
`_stream_body` is now `stream_body`, taking an optional `payload_fn` so a
caller can project each `ChatEvent` to something narrower than the
playground/dashboard's own full shape (see `app/widget/events.py`) before it
is serialized. `app/api/chat.py`'s dashboard route passes no `payload_fn`, so
its behaviour is unchanged -- `event_payload` is still the default.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.service import (
    ChatCitations,
    ChatError,
    ChatEvent,
    ChatMessageEnd,
    ChatMessageStart,
    ChatTextDelta,
    ChatToolCallEnd,
    ChatToolCallStart,
)
from app.conversations.queue import enqueue_title
from app.core.logging import get_logger

logger = get_logger(__name__)

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

#: The three headers `chat_stream` (and now the widget's stream route) sets
#: on every SSE response -- kept as one shared constant so both surfaces
#: agree on the exact same proxy/caching behaviour without copying the
#: literal dict twice.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


class _StreamDone:
    """Sentinel `_pump` puts on the queue once `events` is exhausted.

    A dedicated type (rather than `None` or an `object()` singleton) so
    `isinstance` checks read unambiguously below.
    """

    __slots__ = ()


_STREAM_DONE = _StreamDone()

_QueueItem = ChatEvent | BaseException | _StreamDone
_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]

#: A projection from one `ChatEvent` to its wire shape, or `None` to send
#: nothing for that event at all (e.g. `app/widget/events.py`'s
#: `project_public_event` dropping `tool_call_end` outright). `event_payload`
#: below is the default -- the full, dashboard-facing shape -- and never
#: itself returns `None`.
PayloadFn = Callable[[ChatEvent], dict[str, object] | None]


def event_payload(event: ChatEvent) -> dict[str, object]:
    if isinstance(event, ChatMessageStart):
        return {
            "type": "message_start",
            "conversation_id": str(event.conversation_id),
            "message_id": str(event.message_id),
        }
    if isinstance(event, ChatCitations):
        return {
            "type": "citations",
            "citations": [
                {
                    "chunk_id": str(c.chunk_id) if c.chunk_id is not None else None,
                    "document_id": str(c.document_id) if c.document_id is not None else None,
                    "product_id": str(c.product_id) if c.product_id is not None else None,
                    "document_title": c.document_title,
                    "rank": c.rank,
                    "score": c.score,
                    "excerpt": c.excerpt,
                    "page": c.page,
                }
                for c in event.citations
            ],
        }
    if isinstance(event, ChatTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ChatToolCallStart):
        return {
            "type": "tool_call_start",
            "calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in event.calls],
        }
    if isinstance(event, ChatToolCallEnd):
        return {
            "type": "tool_call_end",
            "results": [
                {
                    "tool_call_id": r.tool_call_id,
                    "tool_name": r.tool_name,
                    "result": r.result,
                    "is_error": r.is_error,
                }
                for r in event.results
            ],
        }
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
    terminated = False
    try:
        async for event in events:
            await queue.put(event)
        await queue.put(_STREAM_DONE)
        terminated = True
    except Exception as exc:  # noqa: BLE001 - forwarded to the consumer below, not swallowed
        await queue.put(exc)
        terminated = True
    finally:
        if not terminated:
            # `events` raised something that is a `BaseException` but not an
            # `Exception` (a bug outside this function's documented
            # contract, but nothing in Python prevents it), or this task was
            # cancelled. Without a terminal item the consumer's
            # `asyncio.wait_for(queue.get(), ...)` loop never breaks: it just
            # emits `: ping` every 15s forever, with no terminal event and no
            # way for the client to learn the turn is over.
            #
            # `put_nowait`, not `await put`: the exception is still in flight
            # (and may be a `CancelledError`, which awaiting here would
            # immediately re-raise), so this must not suspend. The normal and
            # `Exception` paths above therefore keep their blocking `put`,
            # which cannot silently drop the sentinel on a full queue.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_STREAM_DONE)


async def _queue_title(
    conversation_id: uuid.UUID | None, organization_id: uuid.UUID, *, committed: bool
) -> None:
    """Ask for a title for a conversation this turn created.

    `conversation_id` is `None` for every turn that did not create one, so the
    caller's decision is already made by the time this is reached; `committed`
    is the half only the teardown knows.

    Why it waits for the commit: the job runs in the worker process against
    its own session, so it can only see what this turn's transaction actually
    wrote. A rolled-back turn took its conversation row with it, and enqueuing
    then points the worker at a row that no longer exists. That is why the
    call sits *after* `session_cm.__aexit__`, mirroring the upload endpoint,
    which calls `enqueue_ingest` outside its `tenant_session` block for the
    same reason.

    A turn that ended in an in-band `error` event still qualifies: its partial
    reply is deliberately persisted, so the conversation is real, it appears
    in the list, and it needs a label like any other.

    Never allowed to fail the request. The stream is finished and delivered by
    this point, so a Redis outage must not turn a completed answer into a
    failure -- and an untitled conversation still lists under its first
    message.
    """
    if conversation_id is None or not committed:
        return
    try:
        await enqueue_title(conversation_id, organization_id)
    except Exception:  # noqa: BLE001 - logged, never surfaced; see docstring
        logger.warning(
            "conversation_title_enqueue_failed",
            conversation_id=str(conversation_id),
            exc_info=True,
        )


async def stream_body(
    events: AsyncIterator[ChatEvent],
    first_event: ChatEvent,
    session_cm: AbstractAsyncContextManager[AsyncSession],
    title_conversation_id: uuid.UUID | None,
    organization_id: uuid.UUID,
    *,
    payload_fn: PayloadFn = event_payload,
) -> AsyncIterator[bytes]:
    """The actual SSE body, run only after `first_event` has already been
    pulled successfully -- so everything yielded here happens after the 200
    status line is committed, and any failure past this point has nowhere to
    go but an in-band `error` event (already the shape `ChatService.send()`
    yields for a mid-stream provider failure).

    A heartbeat comment is emitted whenever `HEARTBEAT_INTERVAL_SECONDS`
    elapses without a new event, so idle connections are not reaped by a
    proxy while the model is still "thinking".

    `payload_fn`, defaulted to `event_payload`, projects each `ChatEvent`
    before it is serialized -- `None` means that event is skipped outright
    (no SSE frame at all), which is how the widget's `project_public_event`
    drops `tool_call_end` and empty-after-filtering `citations` events. This
    never changes *which* events are tracked for bookkeeping below (e.g.
    `ChatMessageEnd` for the discarded-usage log): tracking reads the event
    itself, not what `payload_fn` made of it.
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
        first_payload = payload_fn(first_event)
        if first_payload is not None:
            yield _sse(first_payload)
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
                chat_error = ChatError(code="internal_error", message=_INTERNAL_ERROR_MESSAGE)
                # `payload_fn` projects this too, so a restricted projection
                # (the widget's) applies to the internal error exactly as it
                # does to every other event -- but an error must never
                # silently vanish, so a `None` here (which neither
                # `event_payload` nor `project_public_event` ever produces
                # for a `ChatError`) falls back to the full payload rather
                # than sending nothing.
                error_payload = payload_fn(chat_error)
                if error_payload is None:
                    error_payload = event_payload(chat_error)
                yield _sse(error_payload)
                break
            if isinstance(item, ChatMessageEnd):
                completed_message_end = item
            payload = payload_fn(item)
            if payload is not None:
                yield _sse(payload)
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
            await _queue_title(
                title_conversation_id, organization_id, committed=exc_info[0] is None
            )
