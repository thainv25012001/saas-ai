from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    TextDelta,
    ThinkingDelta,
)
from anthropic.types import TextBlock as AnthropicTextBlock

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.types import CompletionRequest, Message

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
