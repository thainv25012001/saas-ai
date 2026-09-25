"""Public event projection for anonymous widget visitors
(docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §4.1).

Widget visitors see less than the playground, because tool arguments and
results can carry internal data (lead ids, raw product rows, other chunks)
and costs are the business's own. `project_public_event` is the one pure
function -- no I/O, no database, no network -- that turns a `ChatEvent`
into exactly what the widget's SSE body is allowed to send, or `None` when
the event should not reach the wire at all. `app/api/streaming.py`'s
`stream_body` is what actually calls it, via its `payload_fn` parameter.
"""

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

#: Shown to the visitor in place of whatever `ChatError.message` actually
#: says -- which may itself be internal (a provider's raw error text,
#: `_INTERNAL_ERROR_MESSAGE`'s own reasoning in `app/api/streaming.py`
#: applies here too: a widget visitor gets even less trust than an
#: authenticated dashboard user). `ChatError.code` still travels, since it
#: is a fixed, small vocabulary of stable identifiers (never free text), and
#: the widget UI uses it to decide what to show (e.g. the agent's own
#: `fallback_message` on `widget_daily_cap`).
PUBLIC_ERROR_MESSAGE = "Something went wrong. Please try again."


def project_public_event(event: ChatEvent) -> dict[str, object] | None:
    """Project one `ChatEvent` to what a widget visitor may see, or `None`
    when the event must not be sent at all.

    Per event (spec §4.1):
    - `message_start`, `text_delta`: unchanged.
    - `tool_call_start`: names only (`{calls: [{name}]}`), for a "Searching
      products..." indicator -- never the arguments the model supplied.
    - `tool_call_end`: dropped outright. `ChatToolCallResult.result` is an
      excerpt of whatever the tool returned, which for a widget visitor can
      still be internal (another customer's product data, a raw retrieval
      excerpt) with no use to them once the call has already been reported
      as started.
    - `citations`: `{citations: [{document_title, page}]}`, deduplicated by
      `(document_title, page)` and with product-only citations (no
      `chunk_id`) dropped -- a product citation exists for
      `docs/ARCHITECTURE.md` §5.4's audit trail, not for the widget UI,
      which has no product-detail view to link to. An event left with no
      citations after that filtering becomes `None` rather than an empty
      list, so the UI never renders an empty "Sources" section.
    - `message_end`: `{}` plus its `type` -- none of `usage`, `cost_usd`,
      `model` or `prompt_version_id`, all of which are the business's own
      operating data, not the visitor's.
    - `error`: `{code}` plus `PUBLIC_ERROR_MESSAGE`, never the real message.
    """
    if isinstance(event, ChatMessageStart):
        return {
            "type": "message_start",
            "conversation_id": str(event.conversation_id),
            "message_id": str(event.message_id),
        }
    if isinstance(event, ChatTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ChatToolCallStart):
        return {
            "type": "tool_call_start",
            "calls": [{"name": c.name} for c in event.calls],
        }
    if isinstance(event, ChatToolCallEnd):
        return None
    if isinstance(event, ChatCitations):
        seen: set[tuple[str, int | None]] = set()
        out: list[dict[str, object]] = []
        for c in event.citations:
            if c.chunk_id is None:  # product-only citation
                continue
            key = (c.document_title, c.page)
            if key not in seen:
                seen.add(key)
                out.append({"document_title": c.document_title, "page": c.page})
        return {"type": "citations", "citations": out} if out else None
    if isinstance(event, ChatMessageEnd):
        return {"type": "message_end"}
    if isinstance(event, ChatError):
        return {"type": "error", "code": event.code, "message": PUBLIC_ERROR_MESSAGE}
    raise AssertionError(f"unhandled ChatEvent variant: {event!r}")  # pragma: no cover
