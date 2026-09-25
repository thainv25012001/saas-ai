"""`project_public_event` (`app/widget/events.py`), per spec §4.1: what an
anonymous widget visitor is allowed to see of a `ChatEvent`. Pure function,
no I/O -- so these are all unit tests, one per event variant, plus the
citation dedupe/product-drop rules and a scan proving nothing internal
(tool arguments, tool results, cost, model, usage, excerpts) ever reaches
the serialized wire shape (Review Focus #3).
"""

import json
import uuid
from decimal import Decimal

from app.chat.service import (
    ChatCitations,
    ChatError,
    ChatMessageEnd,
    ChatMessageStart,
    ChatTextDelta,
    ChatToolCall,
    ChatToolCallEnd,
    ChatToolCallResult,
    ChatToolCallStart,
)
from app.llm.types import Usage
from app.rag.retrieve import CitationPayload
from app.widget.events import PUBLIC_ERROR_MESSAGE, project_public_event

_FORBIDDEN_KEYS = ("arguments", "result", "cost_usd", "model", "usage", "excerpt")


def _assert_no_forbidden_keys(payload: object) -> None:
    serialized = json.dumps(payload)
    for key in _FORBIDDEN_KEYS:
        assert f'"{key}"' not in serialized, f"{key!r} leaked into {serialized!r}"


def _citation(
    *,
    chunk_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    document_title: str = "Doc",
    page: int | None = None,
    rank: int = 1,
    score: float = 0.9,
    excerpt: str = "some excerpt",
) -> CitationPayload:
    return CitationPayload(
        chunk_id=chunk_id,
        document_id=uuid.uuid4() if chunk_id is not None else None,
        document_title=document_title,
        rank=rank,
        score=score,
        excerpt=excerpt,
        page=page,
        product_id=product_id,
    )


def test_message_start_is_unchanged() -> None:
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    event = ChatMessageStart(conversation_id=conversation_id, message_id=message_id)
    assert project_public_event(event) == {
        "type": "message_start",
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
    }


def test_text_delta_is_unchanged() -> None:
    event = ChatTextDelta(text="hello")
    assert project_public_event(event) == {"type": "text_delta", "text": "hello"}


def test_tool_call_start_carries_only_names() -> None:
    event = ChatToolCallStart(
        calls=[
            ChatToolCall(id="call_1", name="retrieve_knowledge", arguments={"query": "pricing"}),
            ChatToolCall(id="call_2", name="create_lead", arguments={"email": "a@b.com"}),
        ]
    )
    payload = project_public_event(event)
    assert payload == {
        "type": "tool_call_start",
        "calls": [{"name": "retrieve_knowledge"}, {"name": "create_lead"}],
    }
    _assert_no_forbidden_keys(payload)


def test_tool_call_end_is_dropped() -> None:
    event = ChatToolCallEnd(
        results=[
            ChatToolCallResult(
                tool_call_id="call_1",
                tool_name="retrieve_knowledge",
                result="internal excerpt text",
                is_error=False,
            )
        ]
    )
    payload = project_public_event(event)
    assert payload is None
    _assert_no_forbidden_keys(payload)


def test_message_end_carries_nothing_but_its_type() -> None:
    event = ChatMessageEnd(
        usage=Usage(input_tokens=42, output_tokens=17),
        cost_usd=Decimal("0.0123"),
        latency_ms=250,
        model="claude-sonnet-5",
        prompt_version_id=uuid.uuid4(),
    )
    payload = project_public_event(event)
    assert payload == {"type": "message_end"}
    _assert_no_forbidden_keys(payload)


def test_error_replaces_the_message_with_a_fixed_public_one() -> None:
    event = ChatError(code="llm_unavailable", message="upstream said: SELECT secret FROM tenants")
    payload = project_public_event(event)
    assert payload == {
        "type": "error",
        "code": "llm_unavailable",
        "message": PUBLIC_ERROR_MESSAGE,
    }
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Citations: dedupe, product-only dropped, all-product -> None
# ---------------------------------------------------------------------------


def test_citations_are_deduplicated_by_document_title_and_page() -> None:
    chunk_id = uuid.uuid4()
    event = ChatCitations(
        citations=[
            _citation(chunk_id=chunk_id, document_title="Pricing Guide", page=3),
            _citation(chunk_id=uuid.uuid4(), document_title="Pricing Guide", page=3),
            _citation(chunk_id=uuid.uuid4(), document_title="Pricing Guide", page=4),
        ]
    )
    payload = project_public_event(event)
    assert payload == {
        "type": "citations",
        "citations": [
            {"document_title": "Pricing Guide", "page": 3},
            {"document_title": "Pricing Guide", "page": 4},
        ],
    }
    _assert_no_forbidden_keys(payload)


def test_product_only_citations_are_dropped() -> None:
    event = ChatCitations(
        citations=[
            _citation(chunk_id=uuid.uuid4(), document_title="Pricing Guide", page=1),
            _citation(chunk_id=None, product_id=uuid.uuid4(), document_title="Widget X"),
        ]
    )
    payload = project_public_event(event)
    assert payload == {
        "type": "citations",
        "citations": [{"document_title": "Pricing Guide", "page": 1}],
    }


def test_all_product_citations_yields_none() -> None:
    event = ChatCitations(
        citations=[
            _citation(chunk_id=None, product_id=uuid.uuid4(), document_title="Widget X"),
            _citation(chunk_id=None, product_id=uuid.uuid4(), document_title="Widget Y"),
        ]
    )
    assert project_public_event(event) is None
