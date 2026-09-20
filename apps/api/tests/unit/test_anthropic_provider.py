from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from anthropic.types import (
    InputJSONDelta,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    TextDelta,
    ThinkingDelta,
)
from anthropic.types import TextBlock as AnthropicTextBlock
from anthropic.types import ToolUseBlock as AnthropicToolUseBlock

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.errors import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.types import CompletionRequest, Message, TextBlock, ToolResultBlock, ToolSpec
from app.llm.types import ToolUseBlock as AppToolUseBlock

pytestmark = pytest.mark.anyio


def _request(**overrides) -> CompletionRequest:
    payload = {
        "model": "claude-opus-5",
        "messages": [Message.text("user", "hello")],
        "system": "you are a sales assistant",
        "max_tokens": 1024,
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


class _FakeStream:
    """Stands in for the SDK's async streaming context manager."""

    def __init__(self, events, final):
        self._events, self._final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        async def gen():
            for event in self._events:
                yield event

        return gen()

    async def get_final_message(self):
        return self._final


class _RaisingStream:
    """A stream whose SDK call itself fails — e.g. the HTTP request behind
    `messages.stream(...)` never got a response. Real failures surface here,
    inside `__aenter__`, not partway through iteration."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):  # pragma: no cover - defensive; must never be reached
        raise AssertionError("a stream that failed to open must not be iterated")


def _text_delta(text: str) -> RawContentBlockDeltaEvent:
    """A real SDK event, not a mock: the provider narrows this with
    `isinstance`, which a `MagicMock` can never satisfy."""
    return RawContentBlockDeltaEvent(
        type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text=text)
    )


def _thinking_delta(text: str = "hmm") -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=0,
        delta=ThinkingDelta(type="thinking_delta", thinking=text),
    )


def _content_block_start() -> RawContentBlockStartEvent:
    return RawContentBlockStartEvent(
        type="content_block_start",
        index=0,
        content_block=AnthropicTextBlock(type="text", text=""),
    )


def _tool_use_start(index: int, call_id: str, name: str) -> RawContentBlockStartEvent:
    """The SDK's own `content_block_start` for a `tool_use` block. `input` on
    this event is always `{}` on the wire -- the real arguments arrive after,
    as `input_json_delta` fragments -- so the provider must never read it."""
    return RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=AnthropicToolUseBlock(type="tool_use", id=call_id, name=name, input={}),
    )


def _input_json_delta(index: int, fragment: str) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=InputJSONDelta(type="input_json_delta", partial_json=fragment),
    )


def _content_block_stop(index: int) -> RawContentBlockStopEvent:
    return RawContentBlockStopEvent(type="content_block_stop", index=index)


def _final_message(input_tokens=10, output_tokens=4, stop_reason="end_turn"):
    final = MagicMock()
    final.usage.input_tokens = input_tokens
    final.usage.output_tokens = output_tokens
    final.stop_reason = stop_reason
    return final


def _provider_with(stream):
    provider = AnthropicProvider(api_key="test-key")
    provider._client = MagicMock()  # noqa: SLF001 — substituting the SDK client
    provider._client.messages.stream = MagicMock(return_value=stream)  # noqa: SLF001
    return provider


def _request_object() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limit_error() -> anthropic.RateLimitError:
    response = httpx2.Response(
        429, request=_request_object(), json={"error": {"type": "rate_limit_error"}}
    )
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_request_object())


def _status_error(status_code: int) -> anthropic.APIStatusError:
    response = httpx2.Response(
        status_code, request=_request_object(), json={"error": {"type": "some_error"}}
    )
    return anthropic.APIStatusError("failed", response=response, body=None)


async def test_temperature_is_not_sent_to_a_model_that_rejects_it():
    """THE test for this task. Sending temperature to claude-opus-5 is a 400."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(temperature=0.7)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "temperature" not in kwargs


async def test_temperature_is_sent_to_a_model_that_accepts_it():
    """The counterpart to the test above: without this, the whole temperature
    branch could be deleted and the suite would stay green."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(model="claude-haiku-4-5", temperature=0.7)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["temperature"] == 0.7


async def test_thinking_is_adaptive_for_current_models():
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["thinking"] == {"type": "adaptive"}


async def test_budget_tokens_is_never_sent():
    """It is rejected with a 400 on every model we default to. Checked against
    the WHOLE kwargs payload, not just the `thinking` sub-dict — a
    hardcoded-dict check on one key would miss a `budget_tokens` sent
    anywhere else in the request."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "budget_tokens" not in str(kwargs)


async def test_system_prompt_is_sent_as_the_system_parameter_not_a_message():
    """Anthropic takes the system prompt top-level; putting it in `messages` as a
    system-role entry is rejected."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["system"] == "you are a sales assistant"
    assert all(m["role"] != "system" for m in kwargs["messages"])


async def test_stream_yields_normalized_events_in_order():
    provider = _provider_with(
        _FakeStream([_text_delta("He"), _text_delta("llo")], _final_message())
    )
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_non_text_delta_events_produce_no_text_delta():
    """The stream filter itself, not just its output shape: feed it events
    that are NOT text deltas and confirm none leak through as one. Every event
    in the test above happens to be a text delta, so that test alone cannot
    catch an unconditional yield."""
    provider = _provider_with(
        _FakeStream(
            [_content_block_start(), _thinking_delta(), _text_delta("hi")],
            _final_message(),
        )
    )
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "usage", "message_end"]


async def test_usage_is_taken_from_the_final_message():
    provider = _provider_with(
        _FakeStream([_text_delta("x")], _final_message(input_tokens=99, output_tokens=7))
    )
    end = [e async for e in provider.stream(_request()) if e.type == "message_end"][0]
    assert end.usage.input_tokens == 99
    assert end.usage.output_tokens == 7


async def test_effort_is_forwarded_when_the_model_supports_it():
    provider = _provider_with(_FakeStream([_text_delta("x")], _final_message()))
    async for _ in provider.stream(_request(effort="low")):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["output_config"] == {"effort": "low"}


async def test_effort_is_not_forwarded_when_the_model_does_not_support_it():
    """The counterpart to the test above: without this, `and caps.supports_effort`
    could be deleted from the provider and the suite would stay green."""
    provider = _provider_with(_FakeStream([_text_delta("x")], _final_message()))
    async for _ in provider.stream(_request(model="claude-haiku-4-5", effort="low")):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "output_config" not in kwargs


async def test_generate_reassembles_the_same_text_as_stream():
    """`generate` and `stream` must agree, as they do in `FakeProvider`: this
    is the only test that exercises the `"".join(parts)` reassembly."""
    provider = _provider_with(
        _FakeStream([_text_delta("He"), _text_delta("llo")], _final_message())
    )
    response = await provider.generate(_request())
    assert response.text == "Hello"


async def test_rate_limit_error_is_mapped_to_llm_rate_limit_error():
    provider = _provider_with(_RaisingStream(_rate_limit_error()))
    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream(_request()):
            pass


async def test_timeout_error_is_mapped_to_llm_unavailable_error():
    provider = _provider_with(_RaisingStream(_timeout_error()))
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.stream(_request()):
            pass


async def test_unauthenticated_status_error_is_mapped_to_llm_configuration_error():
    provider = _provider_with(_RaisingStream(_status_error(401)))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass


async def test_bad_request_status_error_is_mapped_to_llm_configuration_error():
    """A 400 is the exact failure this whole task exists to prevent — a
    capability-table miss sending a parameter the model rejects. It must not
    be reported as `LLMUnavailableError`, which reads as transient and
    invites retrying a request that can never succeed."""
    provider = _provider_with(_RaisingStream(_status_error(400)))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass


async def test_server_status_error_is_mapped_to_llm_unavailable_error():
    provider = _provider_with(_RaisingStream(_status_error(503)))
    with pytest.raises(LLMUnavailableError):
        async for _ in provider.stream(_request()):
            pass


async def test_max_tokens_is_clamped_to_the_capability_record():
    """Provider parity: the OpenAI adapter clamps against its own capability
    record too, so neither provider can reach a live 400 for a `max_tokens`
    the model was never going to accept."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    # claude-haiku-4-5 is a `_CLASSIC` record with a 64K ceiling, so a
    # request at the 128K limit `CompletionRequest` itself allows is over it.
    async for _ in provider.stream(_request(model="claude-haiku-4-5", max_tokens=128_000)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["max_tokens"] == provider.capabilities("claude-haiku-4-5").max_output_tokens


async def test_max_tokens_below_the_ceiling_is_forwarded_unchanged():
    """The counterpart to the test above: without this, the clamp could be
    replaced with a hardcoded `max_output_tokens` and the suite would stay
    green."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(max_tokens=256)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["max_tokens"] == 256


async def test_a_stream_with_no_text_raises_llm_empty_response_error():
    """PHASE-2.md §3: "model refused or returned nothing" is
    `LLMEmptyResponseError`. Without this the turn reports success with an
    empty assistant message and a `usage_events` row for it."""
    provider = _provider_with(_FakeStream([_thinking_delta()], _final_message(output_tokens=0)))
    with pytest.raises(LLMEmptyResponseError):
        async for _ in provider.stream(_request()):
            pass


async def test_a_max_tokens_stop_with_no_text_is_not_an_empty_response():
    """Not hypothetical on our own default: `claude-opus-5` runs adaptive
    thinking on by default, so a reasoning-heavy turn can spend the whole
    output budget on hidden reasoning tokens and end with no visible text at
    all. That is a truncation, not a refusal -- it completes normally, empty
    but successful, with its usage recorded. `max_tokens` is the SDK's
    literal for this (verified against `anthropic.types.StopReason`)."""
    provider = _provider_with(
        _FakeStream(
            [_thinking_delta()],
            _final_message(output_tokens=1024, stop_reason="max_tokens"),
        )
    )
    events = [e async for e in provider.stream(_request())]
    assert [e.type for e in events] == ["message_start", "usage", "message_end"]
    end = events[-1]
    assert end.stop_reason == "max_tokens"
    assert end.usage.output_tokens == 1024


async def test_a_tool_stop_with_no_text_is_not_an_empty_response():
    """A model that answers by calling a tool legitimately emits no text.
    Phase 4 wires tool calls up; this pins that the empty-response guard does
    not stand in its way."""
    provider = _provider_with(_FakeStream([], _final_message(stop_reason="tool_use")))
    events = [e async for e in provider.stream(_request())]
    assert [e.type for e in events] == ["message_start", "usage", "message_end"]


def _tools_request(**overrides) -> CompletionRequest:
    tools = [
        ToolSpec(
            name="search",
            description="search stuff",
            input_schema={"type": "object", "properties": {}},
        )
    ]
    return _request(tools=tools, **overrides)


async def test_tool_specs_are_sent_to_the_model():
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_tools_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["tools"] == [
        {
            "name": "search",
            "description": "search stuff",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


async def test_no_tools_key_is_sent_when_the_request_has_none():
    """The counterpart to the test above: without this, the branch could be
    replaced with an unconditional `kwargs["tools"] = []` and the suite would
    stay green."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "tools" not in kwargs


async def test_tool_use_arguments_split_across_three_chunks_are_accumulated():
    """The defect this project keeps finding: a fake that delivers whole JSON
    in one delta tests nothing about the accumulator. This delivers
    `{"order_id": "A1", "confirm": true}` in three fragments, none of which is
    valid JSON on its own."""
    events = [
        _tool_use_start(0, "call_1", "lookup_order"),
        _input_json_delta(0, '{"order_id": '),
        _input_json_delta(0, '"A1", "conf'),
        _input_json_delta(0, 'irm": true}'),
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert len(tool_events) == 1
    block = tool_events[0].block
    assert block.id == "call_1"
    assert block.name == "lookup_order"
    assert block.input == {"order_id": "A1", "confirm": True}


async def test_two_concurrent_tool_calls_are_accumulated_independently():
    """Anthropic interleaves content blocks by index; accumulating into one
    shared buffer instead of per-index would corrupt both calls' JSON."""
    events = [
        _tool_use_start(0, "call_1", "search"),
        _tool_use_start(1, "call_2", "search"),
        _input_json_delta(0, '{"q": "a'),
        _input_json_delta(1, '{"q": "b'),
        _input_json_delta(0, '"}'),
        _input_json_delta(1, '"}'),
        _content_block_stop(0),
        _content_block_stop(1),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert [(e.block.id, e.block.input) for e in tool_events] == [
        ("call_1", {"q": "a"}),
        ("call_2", {"q": "b"}),
    ]


async def test_a_tool_only_turn_streams_no_text_and_does_not_trip_the_empty_response_guard():
    """Requirement: confirm this through the real streamed events
    (content_block_start/delta/stop), not just by handing `stop_reason` to
    `_final_message` directly with no tool content at all -- that would pass
    even against a naive `if not emitted_text: raise` guard, since this test
    would then raise `LLMEmptyResponseError` and fail."""
    events = [
        _tool_use_start(0, "call_1", "lookup_order"),
        _input_json_delta(0, "{}"),
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    result = [e async for e in provider.stream(_request())]
    assert [e.type for e in result] == ["message_start", "tool_use", "usage", "message_end"]
    end = result[-1]
    assert end.stop_reason == "tool_use"


async def test_an_input_json_delta_for_an_index_with_no_pending_call_does_not_crash():
    """Two shapes of the same wire anomaly, neither of which we cause: a
    fragment for an index that never had a `content_block_start` at all, and
    a fragment that arrives AFTER that index's `content_block_stop` already
    popped it (the exact case the brief names). Before the `.get` guard,
    `pending_tool_calls[raw_event.index]` raised `KeyError` straight out of
    `stream()`, uncaught by any of the `except` clauses below it."""
    events = [
        _input_json_delta(9, '{"orphaned": true}'),  # index 9 never started
        _tool_use_start(0, "call_1", "lookup_order"),
        _input_json_delta(0, "{}"),
        _content_block_stop(0),
        _input_json_delta(0, '{"too_late": true}'),  # arrives after the stop
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].block.id == "call_1"
    assert tool_events[0].block.input == {}


async def test_a_fragment_arriving_before_its_own_call_starts_is_dropped_not_crashed():
    """The `.get` guard's blind spot: a fragment for index 0 that arrives
    BEFORE that index's own `content_block_start` is correctly dropped as
    "never started", since nothing is pending for it yet -- but that drop
    means the LATER fragments for the same call, once it does start, no
    longer add up to valid JSON. Before wrapping the parse in try/except,
    this raised an uncaught `JSONDecodeError` out of `stream()` with the same
    blast radius as the original `KeyError`."""
    events = [
        _input_json_delta(0, '{"order_id": '),  # arrives before its own start
        _tool_use_start(0, "call_1", "lookup_order"),
        _input_json_delta(0, '"A1"}'),
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    # The malformed call is dropped entirely -- no `tool_use` event for it --
    # rather than the whole turn (or the process) crashing on it.
    assert tool_events == []


@pytest.mark.parametrize(
    "fragments",
    [
        pytest.param(["42"], id="scalar-number"),
        pytest.param(['"hello"'], id="scalar-string"),
        pytest.param(["null"], id="scalar-null"),
        pytest.param(["[1, ", "2, 3]"], id="array"),
    ],
)
async def test_valid_but_non_object_json_is_dropped_not_crashed(fragments):
    """`json.loads` happily parses `42`, `"hello"`, `null`, and `[1, 2, 3]` --
    none of them raise `JSONDecodeError` -- so a guard scoped to that one
    exception lets every one of these through to `ToolUseBlock(input=...)`,
    which then raises `pydantic.ValidationError` because `input` must be a
    `dict`. That error must be caught by the SAME guard as malformed JSON
    syntax, not a fourth `except` clause bolted on next to it."""
    events = [
        _tool_use_start(0, "call_1", "lookup_order"),
        *[_input_json_delta(0, fragment) for fragment in fragments],
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert tool_events == []


async def test_one_malformed_tool_call_does_not_take_down_a_well_formed_one():
    """The whole point of dropping rather than raising: a malformed call
    sharing a turn with a well-formed one must not cost the well-formed
    call its `ToolUseEvent`."""
    events = [
        _tool_use_start(0, "call_1", "broken"),
        _input_json_delta(0, "42"),  # valid JSON, not an object -- dropped
        _content_block_stop(0),
        _tool_use_start(1, "call_2", "search"),
        _input_json_delta(1, '{"q": "x"}'),
        _content_block_stop(1),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].block.id == "call_2"
    assert tool_events[0].block.input == {"q": "x"}


async def test_tool_use_with_no_arguments_parses_as_an_empty_dict():
    """A tool called with no arguments streams zero `input_json_delta`
    fragments at all -- `json.loads("")` raises, so this edge (as distinct
    from `{}` arriving as an explicit fragment above) must be handled too."""
    events = [
        _tool_use_start(0, "call_1", "ping"),
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    tool_events = [e async for e in provider.stream(_request()) if e.type == "tool_use"]
    assert tool_events[0].block.input == {}


async def test_generate_includes_tool_use_blocks_in_content():
    events = [
        _tool_use_start(0, "call_1", "lookup_order"),
        _input_json_delta(0, '{"order_id": "A1"}'),
        _content_block_stop(0),
    ]
    provider = _provider_with(_FakeStream(events, _final_message(stop_reason="tool_use")))
    response = await provider.generate(_request())
    tool_blocks = [b for b in response.content if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].name == "lookup_order"
    assert tool_blocks[0].input == {"order_id": "A1"}


def _tool_round_trip_messages() -> list[Message]:
    """The exact message shape `AgentRunner.run` builds on step two of a
    tool-using turn: the original user text, an assistant turn carrying
    text PLUS two `ToolUseBlock`s (the multi-call case, since that is where
    a naive implementation is most likely to only handle one), and a user
    turn carrying the two matching `ToolResultBlock`s -- one of them an
    error, since `is_error` must round-trip too."""
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
    degraded to `[{'role':'user','content':'find shoes'},
    {'role':'assistant','content':''}, {'role':'user','content':''}]` --
    a 400 against the real API for the empty-text assistant turn, or, if it
    weren't, a tool result that never reaches the model at all. This builds
    the message list `AgentRunner` actually produces on step two and asserts
    every block survives serialization, matched to its call by id."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(messages=_tool_round_trip_messages())):
        pass
    messages = provider._client.messages.stream.call_args.kwargs["messages"]  # noqa: SLF001

    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "find shoes"}]}

    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "call_1", "name": "search_products", "input": {"q": "shoes"}},
        {"type": "tool_use", "id": "call_2", "name": "search_products", "input": {"q": "boots"}},
    ]

    tool_results = messages[2]
    assert tool_results["role"] == "user"
    assert tool_results["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "3 results",
            "is_error": False,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_2",
            "content": "no results",
            "is_error": True,
        },
    ]


async def test_an_entirely_empty_assistant_message_is_dropped_not_sent():
    """Anthropic rejects an empty-content message outright, on any role,
    anywhere in the array. `AgentRunner` produces exactly this shape when a
    step's outcome has neither text (`outcome.text == ""`) nor tool calls."""
    messages = [
        Message.text("user", "hi"),
        Message(role="assistant", content=[TextBlock(text="")]),
        Message.text("user", "still there?"),
    ]
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(messages=messages)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["user", "user"]  # the empty assistant turn is gone entirely


def test_capabilities_report_no_sampling_for_current_models():
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-opus-5").supports_sampling is False


def test_capabilities_report_sampling_for_older_models():
    """Haiku 4.5 still accepts temperature; the table must not over-generalize."""
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-haiku-4-5").supports_sampling is True


def test_unknown_model_falls_back_to_the_conservative_capability_set():
    """Guessing 'it probably supports sampling' would produce a 400 on a new model.
    The safe default is to send less."""
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-something-new").supports_sampling is False
