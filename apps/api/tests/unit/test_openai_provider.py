from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest

from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.openai_provider import OpenAIProvider
from app.llm.types import CompletionRequest, Message

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


def _chunk(text=None, usage=None, finish_reason=None):
    chunk = MagicMock()
    if usage is None:
        chunk.usage = None
    else:
        chunk.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    if text is None and finish_reason is None:
        chunk.choices = []
    else:
        choice = MagicMock()
        choice.delta.content = text
        choice.finish_reason = finish_reason
        chunk.choices = [choice]
    return chunk


def _provider_with(chunks):
    """Stands in for the SDK's `create(..., stream=True)`.

    `AsyncCompletions.create` is a real `async def` — calling it returns a
    coroutine, and only *awaiting* that coroutine yields the async-iterable
    stream (verified against the installed SDK: calling it without awaiting
    produces a bare `coroutine` object with no `__aiter__`). A plain
    `MagicMock` whose `side_effect` returns an async generator directly would
    make `await create(...)` raise `TypeError: object async_generator can't
    be used in 'await' expression` — which would only be caught by writing
    provider code that skips the `await`, and that code would then be unable
    to iterate the real SDK's coroutine return value in production. `AsyncMock`
    reproduces the real shape: calling it returns a coroutine, and awaiting
    that coroutine runs `side_effect` and returns the async generator.
    """

    def _aiter(**_kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()

    provider = OpenAIProvider(api_key="test-key")
    create = AsyncMock(side_effect=_aiter)
    provider._client = MagicMock()  # noqa: SLF001
    provider._client.chat.completions.create = create  # noqa: SLF001
    return provider


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
    """The final usage-only chunk has an empty `choices` list."""
    provider = _provider_with([_chunk(usage=(1, 1))])
    events = [e async for e in provider.stream(_request())]
    assert any(e.type == "message_end" for e in events)


async def test_a_none_content_delta_is_skipped():
    """Role-only opening deltas carry content=None."""
    provider = _provider_with([_chunk(None, finish_reason=None), _chunk("x", finish_reason="stop")])
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


async def test_generate_structured_is_not_implemented_yet():
    provider = OpenAIProvider(api_key="k")
    with pytest.raises(NotImplementedError):
        await provider.generate_structured(_request(), Message)
