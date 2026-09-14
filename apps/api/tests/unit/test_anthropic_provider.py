from unittest.mock import MagicMock

import pytest

from app.llm.anthropic_provider import AnthropicProvider
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


def _text_delta(text):
    event = MagicMock()
    event.type = "content_block_delta"
    event.delta.type = "text_delta"
    event.delta.text = text
    return event


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


async def test_temperature_is_not_sent_to_a_model_that_rejects_it():
    """THE test for this task. Sending temperature to claude-opus-5 is a 400."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(temperature=0.7)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "temperature" not in kwargs


async def test_thinking_is_adaptive_for_current_models():
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["thinking"] == {"type": "adaptive"}


async def test_budget_tokens_is_never_sent():
    """It is rejected with a 400 on every model we default to."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "budget_tokens" not in str(kwargs.get("thinking", {}))


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
