from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest

from app.llm.errors import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.openai_provider import OpenAIProvider
from app.llm.types import CompletionRequest, Message, TextBlock, ToolResultBlock, ToolSpec
from app.llm.types import ToolUseBlock as AppToolUseBlock

from ._llm_stubs import chunk, streaming, tool_call_delta

pytestmark = pytest.mark.anyio


def _request(**overrides) -> CompletionRequest:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [Message.text("user", "hello")],
        "system": "you are a sales assistant",
        "max_tokens": 512,
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


def _chunk(text=None, usage=None, finish_reason=None, tool_calls=None):
    return chunk(text=text, usage=usage, finish_reason=finish_reason, tool_calls=tool_calls)


def _provider_with(chunks):
    return streaming(OpenAIProvider(api_key="test-key"), chunks)


def _request_object() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(
        429, request=_request_object(), json={"error": {"type": "rate_limit_error"}}
    )
    return openai.RateLimitError("rate limited", response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_request_object())


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_request_object())


def _status_error(status_code: int) -> openai.APIStatusError:
    response = httpx2.Response(
        status_code, request=_request_object(), json={"error": {"type": "some_error"}}
    )
    return openai.APIStatusError("failed", response=response, body=None)


def _raising_provider(exc: Exception) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="test-key")
    provider._client = MagicMock()  # noqa: SLF001
    provider._client.chat.completions.create = AsyncMock(side_effect=exc)  # noqa: SLF001
    return provider


async def test_system_prompt_becomes_the_first_message():
    """OpenAI has no top-level system parameter — it is a message with role=system."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    messages = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    assert messages[0] == {"role": "system", "content": "you are a sales assistant"}
    assert messages[1]["role"] == "user"


async def test_temperature_is_forwarded_because_openai_accepts_it():
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(temperature=0.7)):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["temperature"] == 0.7


async def test_temperature_is_omitted_when_not_requested():
    """The counterpart to the test above: without this, the whole
    `if request.temperature is not None` branch could be replaced with an
    unconditional `kwargs["temperature"] = request.temperature` and the suite
    would stay green."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert "temperature" not in kwargs


async def test_usage_is_requested_explicitly():
    """OpenAI omits usage from streamed responses unless asked."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["stream_options"] == {"include_usage": True}


async def test_stream_is_set_and_stream_param_forwarded():
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["stream"] is True
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 512


async def test_stream_yields_normalized_events_in_order():
    provider = _provider_with(
        [_chunk("He"), _chunk("llo", finish_reason="stop"), _chunk(usage=(12, 3))]
    )
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_usage_reaches_message_end():
    provider = _provider_with([_chunk("x", finish_reason="stop"), _chunk(usage=(12, 3))])
    end = [e async for e in provider.stream(_request()) if e.type == "message_end"][0]
    assert end.usage.input_tokens == 12
    assert end.usage.output_tokens == 3


async def test_chunks_with_no_choices_do_not_crash_the_stream():
    """The final usage-only chunk has an empty `choices` list -- indexing
    into it unconditionally raises IndexError on every single request."""
    provider = _provider_with([_chunk("x", finish_reason="stop"), _chunk(usage=(1, 1))])
    events = [e async for e in provider.stream(_request())]
    assert any(e.type == "message_end" for e in events)


async def test_a_none_content_delta_is_skipped():
    """Role-only opening deltas carry content=None.

    `finish_reason="stop"` on the first chunk is load-bearing: `_chunk()`
    builds an EMPTY `choices` list when both `text` and `finish_reason` are
    `None`, so `_chunk(None, finish_reason=None)` would silently re-test the
    empty-choices case above instead of a real choice whose `content` is
    `None`.
    """
    provider = _provider_with(
        [_chunk(None, finish_reason="stop"), _chunk("x", finish_reason="stop")]
    )
    text = "".join([e.text async for e in provider.stream(_request()) if e.type == "text_delta"])
    assert text == "x"


async def test_an_empty_string_content_delta_is_also_skipped():
    """`choice.delta.content` can be `""` as well as `None`; neither is real text."""
    provider = _provider_with([_chunk("", finish_reason=None), _chunk("x", finish_reason="stop")])
    events = [e async for e in provider.stream(_request()) if e.type == "text_delta"]
    assert len(events) == 1
    assert events[0].text == "x"


async def test_generate_reassembles_the_same_text_as_stream():
    """`generate` and `stream` must agree, as they do for the Anthropic provider."""
    provider = _provider_with([_chunk("He"), _chunk("llo", finish_reason="stop")])
    response = await provider.generate(_request())
    assert response.text == "Hello"


async def test_rate_limit_error_is_mapped_to_llm_rate_limit_error():
    provider = _raising_provider(_rate_limit_error())
    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream(_request()):
            pass


async def test_timeout_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_timeout_error())
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.stream(_request()):
            pass


async def test_connection_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_connection_error())
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.stream(_request()):
            pass


async def test_unauthenticated_status_error_is_mapped_to_llm_configuration_error():
    provider = _raising_provider(_status_error(401))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass


async def test_forbidden_status_error_is_mapped_to_llm_configuration_error():
    provider = _raising_provider(_status_error(403))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass


async def test_bad_request_status_error_is_mapped_to_llm_configuration_error():
    """A 400 must not be reported as `LLMUnavailableError`, which reads as
    transient and invites retrying a request that can never succeed."""
    provider = _raising_provider(_status_error(400))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass


async def test_server_status_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_status_error(503))
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.stream(_request()):
            pass


def _tool_round_trip_messages() -> list[Message]:
    """The exact message shape `AgentRunner.run` builds on step two of a
    tool-using turn: the original user text, an assistant turn carrying
    text PLUS two `ToolUseBlock`s (the multi-call case, since that is where
    OpenAI's one-message-per-result shape diverges most from Anthropic's),
    and a user turn carrying the two matching `ToolResultBlock`s -- one of
    them an error."""
    return [
        Message.text("user", "find shoes"),
        Message(
            role="assistant",
            content=[
                TextBlock(text="Let me check."),
                AppToolUseBlock(id="call_1", name="search_products", input={"q": "shoes"}),
                AppToolUseBlock(id="call_2", name="search_products", input={"q": "boots"}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="call_1", content="3 results", is_error=False),
                ToolResultBlock(tool_use_id="call_2", content="no results", is_error=True),
            ],
        ),
    ]


async def test_tool_round_trip_serializes_every_block_not_just_text():
    """THE bug Task 4's reviewer found: `Message.text_content` silently
    discards every `ToolUseBlock`/`ToolResultBlock`, so a step-two request
    degraded to an empty-content assistant turn and an empty-content user
    turn -- the tool result never reaching the model, so it would
    re-request the same tool every step until the cap. This builds the
    message list `AgentRunner` actually produces on step two: an assistant
    message with a `tool_calls` array (not nested content blocks, unlike
    Anthropic), and each tool result as its OWN `role: "tool"` message."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(messages=_tool_round_trip_messages())):
        pass
    messages = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "find shoes"}

    assistant = messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me check."
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_products", "arguments": '{"q": "shoes"}'},
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "search_products", "arguments": '{"q": "boots"}'},
        },
    ]

    # Each result is its OWN message -- the shape that diverges most from
    # Anthropic's single nested-content-block user turn. `call_2`'s result
    # is `is_error=True` and picks up the "Error: " prefix this format needs
    # to carry that signal at all (no dedicated field, unlike Anthropic's
    # `tool_result.is_error`) -- see the dedicated distinguishability tests
    # below for why that prefix exists.
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "3 results"}
    assert messages[4] == {
        "role": "tool",
        "tool_call_id": "call_2",
        "content": "Error: no results",
    }
    assert len(messages) == 5


async def test_an_entirely_empty_assistant_message_is_dropped_not_sent():
    """Kept aligned with the Anthropic adapter's own guard against this
    shape: `AgentRunner` produces an assistant turn with neither text
    (`outcome.text == ""`) nor tool calls whenever a step's outcome is
    truly empty, and Anthropic outright rejects that content -- dropping it
    here too means the same conversation history behaves the same way
    against both providers instead of one silently accepting it."""
    messages = [
        Message.text("user", "hi"),
        Message(role="assistant", content=[TextBlock(text="")]),
        Message.text("user", "still there?"),
    ]
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(messages=messages)):
        pass
    sent = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    roles = [m["role"] for m in sent]
    assert roles == ["system", "user", "user"]  # the empty assistant turn is gone


async def test_a_failed_tool_result_is_rendered_distinguishably_from_success():
    """This wire format has no `is_error` field the way Anthropic's
    `tool_result` block does, so the signal must survive in `content`
    itself -- ARCHITECTURE.md §7.3 requires a failed tool reach the model as
    a readable failure, not fiction. `ToolResult(content="", is_error=True)`
    is legal for a tool author to return today, and without a prefix it
    would render byte-for-byte identical to an empty SUCCESS
    (`{"role": "tool", "tool_call_id": "c1", "content": ""}` either way).
    Both the general case and the empty-content edge (a bare "Error: " with
    nothing after it reads as a rendering glitch, not a clear failure
    signal) are pinned here."""
    messages = [
        Message.text("user", "find shoes"),
        Message(
            role="assistant",
            content=[AppToolUseBlock(id="call_1", name="search", input={"q": "shoes"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call_1", content="", is_error=True)],
        ),
    ]
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(messages=messages)):
        pass
    sent = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    tool_message = next(m for m in sent if m["role"] == "tool")
    assert tool_message["content"] != ""
    assert "error" in tool_message["content"].lower()


async def test_a_successful_empty_tool_result_is_not_marked_as_an_error():
    """The counterpart to the test above: without this, `is_error` could be
    ignored entirely and every result -- success included -- could be
    prefixed as an error, and the suite would still catch the failure case."""
    messages = [
        Message.text("user", "find shoes"),
        Message(
            role="assistant",
            content=[AppToolUseBlock(id="call_1", name="search", input={"q": "shoes"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call_1", content="", is_error=False)],
        ),
    ]
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(messages=messages)):
        pass
    sent = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    tool_message = next(m for m in sent if m["role"] == "tool")
    assert tool_message["content"] == ""


def test_capabilities_report_sampling_support():
    provider = OpenAIProvider(api_key="k")
    caps = provider.capabilities("gpt-4o-mini")
    assert caps.supports_sampling is True
    assert caps.supports_thinking is False


def test_capabilities_are_the_same_for_every_model_name():
    """A single permissive record is correct here — OpenAI did not remove
    sampling from any current chat model, unlike Anthropic's per-model table."""
    provider = OpenAIProvider(api_key="k")
    assert provider.capabilities("gpt-4o-mini") == provider.capabilities("gpt-4-turbo")


async def test_max_tokens_is_clamped_to_the_capability_record():
    """Provider parity: the Anthropic adapter clamps against its own
    capability record, so this one must too. `CreateAgentInput.max_tokens`
    allows up to 32_000 and the dashboard exposes it, so an agent with
    max_tokens=24000 is reachable -- forwarded raw it is a live 400 ->
    `LLMConfigurationError` -> HTTP 500, while the identical agent on
    `claude-opus-5` is silently clamped and succeeds."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(max_tokens=24_000)):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["max_tokens"] == provider.capabilities("gpt-4o-mini").max_output_tokens


async def test_max_tokens_below_the_ceiling_is_forwarded_unchanged():
    """The counterpart to the test above: without this, the clamp could be
    replaced with a hardcoded `max_output_tokens` and the suite would stay
    green."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(max_tokens=256)):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["max_tokens"] == 256


async def test_a_stream_with_no_text_raises_llm_empty_response_error():
    """PHASE-2.md §3: "model refused or returned nothing" is
    `LLMEmptyResponseError`. Without this the turn reports success with an
    empty assistant message and a `usage_events` row for it."""
    provider = _provider_with([_chunk(None, finish_reason="stop"), _chunk(usage=(12, 0))])
    with pytest.raises(LLMEmptyResponseError):
        async for _ in provider.stream(_request()):
            pass


async def test_a_length_stop_with_no_text_is_not_an_empty_response():
    """A turn truncated by the output budget is not a refusal. Reporting
    "the model returned nothing" for it sends whoever is debugging it looking
    for a content filter when the actual fix is a larger `max_tokens` -- so
    it completes normally, empty but successful, with its usage recorded.
    `length` is the SDK's literal for this (verified against
    `openai.types.chat.chat_completion_chunk.Choice.finish_reason`)."""
    provider = _provider_with([_chunk(None, finish_reason="length"), _chunk(usage=(12, 900))])
    events = [e async for e in provider.stream(_request())]
    assert [e.type for e in events] == ["message_start", "usage", "message_end"]
    end = events[-1]
    assert end.stop_reason == "length"
    assert end.usage.output_tokens == 900


async def test_a_tool_stop_with_no_text_is_not_an_empty_response():
    """A model that answers by calling a tool legitimately emits no text.
    Phase 4 wires tool calls up; this pins that the empty-response guard does
    not stand in its way."""
    provider = _provider_with([_chunk(None, finish_reason="tool_calls"), _chunk(usage=(12, 4))])
    events = [e async for e in provider.stream(_request())]
    assert [e.type for e in events] == ["message_start", "usage", "message_end"]


async def test_generate_structured_is_not_implemented_yet():
    provider = OpenAIProvider(api_key="k")
    with pytest.raises(NotImplementedError):
        await provider.generate_structured(_request(), Message)


async def test_openai_is_sent_no_vendor_extras():
    """The counterpart to `test_reasoning_is_disabled_...` in
    `test_openrouter_provider.py`: the `reasoning` body is OpenRouter's own
    extension, and OpenAI rejects unknown body fields. Without this, moving the
    flag from the subclass onto the shared class would go unnoticed."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs.get("extra_body") is None


def _tools_request(**overrides):
    tools = [
        ToolSpec(
            name="search",
            description="search stuff",
            input_schema={"type": "object", "properties": {}},
        )
    ]
    return _request(tools=tools, **overrides)


async def test_tool_specs_are_sent_in_the_request():
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_tools_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "search stuff",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


async def test_no_tools_key_is_sent_when_the_request_has_none():
    """The counterpart to the test above: without this, the branch could send
    an empty list (or omit the field entirely from a different code path) and
    the suite would stay green. `openai.omit` is what "not provided" looks
    like in this SDK -- a plain `None` is a different, disallowed value for
    this parameter."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["tools"] is openai.omit


async def test_tool_call_arguments_split_across_three_chunks_are_accumulated():
    """The defect this project keeps finding: a fake that delivers whole JSON
    in one delta tests nothing about the accumulator. This delivers
    `{"order_id": "A1", "confirm": true}` in three fragments, none of which is
    valid JSON on its own."""
    provider = _provider_with(
        [
            _chunk(tool_calls=[tool_call_delta(0, id="call_1", name="lookup_order", arguments="")]),
            _chunk(tool_calls=[tool_call_delta(0, arguments='{"order_id": ')]),
            _chunk(tool_calls=[tool_call_delta(0, arguments='"A1", "conf')]),
            _chunk(tool_calls=[tool_call_delta(0, arguments='irm": true}')]),
            _chunk(finish_reason="tool_calls"),
            _chunk(usage=(10, 4)),
        ]
    )
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert len(tool_events) == 1
    block = tool_events[0].block
    assert block.id == "call_1"
    assert block.name == "lookup_order"
    assert block.input == {"order_id": "A1", "confirm": True}


async def test_two_concurrent_tool_calls_are_accumulated_independently():
    """OpenAI's fragments are keyed by `index` precisely because a model can
    ask for several tools at once; accumulating into one shared buffer
    instead of per-index would corrupt both calls' JSON."""
    provider = _provider_with(
        [
            _chunk(
                tool_calls=[
                    tool_call_delta(0, id="call_1", name="search", arguments=""),
                    tool_call_delta(1, id="call_2", name="search", arguments=""),
                ]
            ),
            _chunk(
                tool_calls=[
                    tool_call_delta(0, arguments='{"q": "a'),
                    tool_call_delta(1, arguments='{"q": "b'),
                ]
            ),
            _chunk(
                tool_calls=[
                    tool_call_delta(0, arguments='"}'),
                    tool_call_delta(1, arguments='"}'),
                ]
            ),
            _chunk(finish_reason="tool_calls"),
            _chunk(usage=(10, 4)),
        ]
    )
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert [(e.block.id, e.block.input) for e in tool_events] == [
        ("call_1", {"q": "a"}),
        ("call_2", {"q": "b"}),
    ]


async def test_a_tool_only_turn_streams_no_text_and_does_not_trip_the_empty_response_guard():
    """Confirmed through real streamed tool-call deltas, not just by handing
    `finish_reason="tool_calls"` to a chunk with no tool content at all --
    that would pass even against a naive `if not emitted_text: raise` guard,
    since this test would then raise `LLMEmptyResponseError` and fail."""
    provider = _provider_with(
        [
            _chunk(
                tool_calls=[tool_call_delta(0, id="call_1", name="lookup_order", arguments="{}")]
            ),
            _chunk(finish_reason="tool_calls"),
            _chunk(usage=(10, 2)),
        ]
    )
    events = [e async for e in provider.stream(_request())]
    assert [e.type for e in events] == ["message_start", "tool_use", "usage", "message_end"]
    end = events[-1]
    assert end.stop_reason == "tool_calls"


async def test_concurrent_tool_calls_are_finalized_in_index_order_even_when_fragments_are_not():
    """The wire is under no obligation to send index 0's fragments before
    index 1's. `sorted(pending_tool_calls)` in the provider is what turns
    arrival order into deterministic call order -- without it, this test
    passes or fails depending on dict insertion order, which happens to
    match ascending index today only because every other test in this file
    sends fragments in ascending order."""
    provider = _provider_with(
        [
            _chunk(
                tool_calls=[
                    tool_call_delta(1, id="call_2", name="search", arguments=""),
                    tool_call_delta(0, id="call_1", name="search", arguments=""),
                ]
            ),
            _chunk(
                tool_calls=[
                    tool_call_delta(1, arguments='{"q": "b'),
                    tool_call_delta(0, arguments='{"q": "a'),
                ]
            ),
            _chunk(
                tool_calls=[
                    tool_call_delta(1, arguments='"}'),
                    tool_call_delta(0, arguments='"}'),
                ]
            ),
            _chunk(finish_reason="tool_calls"),
            _chunk(usage=(10, 4)),
        ]
    )
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert [(e.block.id, e.block.input) for e in tool_events] == [
        ("call_1", {"q": "a"}),
        ("call_2", {"q": "b"}),
    ]


async def test_generate_includes_tool_use_blocks_in_content():
    provider = _provider_with(
        [
            _chunk(
                tool_calls=[
                    tool_call_delta(
                        0, id="call_1", name="lookup_order", arguments='{"order_id": "A1"}'
                    )
                ]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    response = await provider.generate(_request())
    tool_blocks = [b for b in response.content if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].name == "lookup_order"
    assert tool_blocks[0].input == {"order_id": "A1"}
