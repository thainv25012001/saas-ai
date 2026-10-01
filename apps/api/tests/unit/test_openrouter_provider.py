"""OpenRouter speaks OpenAI's wire format, so `OpenRouterProvider` inherits
the whole stream loop and error mapping from `OpenAIProvider` -- which
`test_openai_provider.py` already covers exhaustively. These tests cover only
what is genuinely different: where the requests go, what the provider calls
itself, and one end-to-end smoke test proving the inherited loop really does
run when driven through this subclass.
"""

from unittest.mock import AsyncMock

import pytest

from app.llm.errors import LLMConfigurationError
from app.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider
from app.llm.types import CompletionRequest, Message, ToolResultBlock, ToolSpec
from app.llm.types import ToolUseBlock as AppToolUseBlock

from ._llm_stubs import chunk, scripted, status_error, streaming

pytestmark = pytest.mark.anyio


def _request(**overrides) -> CompletionRequest:
    payload = {
        "model": "z-ai/glm-5.2:free",
        "messages": [Message.text("user", "hello")],
        "system": "you are a sales assistant",
        "max_tokens": 512,
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


def _chunk(text=None, usage=None, finish_reason=None):
    return chunk(text=text, usage=usage, finish_reason=finish_reason)


#: Stands in for the live free-model roster, so no test ever reaches the network.
_FALLBACKS = ["google/gemma-4-31b-it:free", "nex-agi/nex-n2.5-mini:free", "poolside/x:free"]


def _provider(fallback_ids=_FALLBACKS) -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="test-key", fallback_models=AsyncMock(return_value=fallback_ids)
    )


def _provider_with(chunks) -> OpenRouterProvider:
    return streaming(_provider(), chunks)


def _model_rejected(status_code=400):
    return status_error(status_code, body={"error": {"message": "not a valid model ID"}})


def _sent_models(provider: OpenRouterProvider) -> list[str]:
    calls = provider._client.chat.completions.create.call_args_list  # noqa: SLF001
    return [call.kwargs["model"] for call in calls]


def test_requests_go_to_openrouter_not_openai():
    """The single most important difference. Without the base URL override
    this class is an OpenAI client holding an OpenRouter key, which fails
    authentication on every request."""
    provider = OpenRouterProvider(api_key="test-key")
    assert str(provider._client.base_url).rstrip("/") == OPENROUTER_BASE_URL  # noqa: SLF001


def test_it_reports_itself_as_openrouter():
    """`agents.provider` is persisted from this name and `usage_events.provider`
    is reported from it -- inheriting "openai" would mislabel every row."""
    assert OpenRouterProvider(api_key="test-key").name == "openrouter"


async def test_the_inherited_stream_loop_normalizes_events():
    provider = _provider_with(
        [_chunk("He"), _chunk("llo", finish_reason="stop"), _chunk(usage=(12, 3))]
    )
    events = [event async for event in provider.stream(_request())]
    assert [e.type for e in events] == [
        "message_start",
        "text_delta",
        "text_delta",
        "usage",
        "message_end",
    ]
    end = events[-1]
    assert end.usage.input_tokens == 12
    assert end.usage.output_tokens == 3
    assert end.model == "z-ai/glm-5.2:free"


async def test_tool_specs_flow_through_the_inherited_stream_loop():
    """Tool-sending lives entirely in `OpenAIProvider._tools`, inherited here
    unchanged. One smoke test proves it really is reached through this
    subclass -- the accumulation itself is exhaustively covered in
    `test_openai_provider.py`."""
    tools = [
        ToolSpec(name="search", description="d", input_schema={"type": "object", "properties": {}})
    ]
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(tools=tools)):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["tools"][0]["function"]["name"] == "search"


async def test_tool_round_trip_flows_through_the_inherited_message_rendering():
    """Message rendering (`_assistant_message`/`_user_messages`) lives
    entirely in `OpenAIProvider`, inherited here unchanged. One smoke test
    proves an assistant turn's `tool_calls` and a matching tool-result
    message both survive through this subclass -- exhaustive coverage of
    the rendering itself is `test_openai_provider.py`'s job."""
    messages = [
        Message.text("user", "find shoes"),
        Message(
            role="assistant",
            content=[AppToolUseBlock(id="call_1", name="search", input={"q": "shoes"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call_1", content="3 results")],
        ),
    ]
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(messages=messages)):
        pass
    sent = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "search"
    tool_result = next(m for m in sent if m["role"] == "tool")
    assert tool_result == {"role": "tool", "tool_call_id": "call_1", "content": "3 results"}


async def test_reasoning_is_disabled_so_the_model_answers_in_prose():
    """Most of OpenRouter's free roster are reasoning models. OpenRouter returns
    their thinking on a `reasoning` field the OpenAI-compatible `content` never
    carries, so left alone they spend the whole output budget thinking and
    stream an EMPTY reply that stops at `length`. Verified against the live API:
    `nvidia/nemotron-3.5-lightning:free` answered with 200 tokens of visible
    thinking and no answer until this flag was sent, then answered in 29."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["extra_body"]["reasoning"] == {"enabled": False}


async def test_openrouter_is_given_fallback_models_to_route_to():
    """OpenRouter's own `models` routing: if the primary cannot serve (down,
    rate limited, refused), OpenRouter tries the next without a second round
    trip from us. The primary leads, and never appears twice."""
    provider = streaming(
        _provider(["z-ai/glm-5.2:free", *_FALLBACKS]), [_chunk("hi", finish_reason="stop")]
    )
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["extra_body"]["models"] == ["z-ai/glm-5.2:free", *_FALLBACKS[:2]]


async def test_a_400_on_the_primary_retries_once_on_a_fallback_model():
    """The widget's real failure: the agent's saved free model was retired,
    and OpenRouter 400s the request before a single token streams."""
    provider = scripted(
        _provider(), _model_rejected(), [_chunk("hi", finish_reason="stop"), _chunk(usage=(5, 1))]
    )
    events = [event async for event in provider.stream(_request())]

    assert _sent_models(provider) == ["z-ai/glm-5.2:free", _FALLBACKS[0]]
    assert [e.type for e in events] == ["message_start", "text_delta", "usage", "message_end"]
    # What answered is what gets recorded -- not the model that 400ed.
    assert events[0].model == _FALLBACKS[0]
    assert events[-1].model == _FALLBACKS[0]
    # The fallback's own routing list must not lead back to the dead model.
    retry_kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert "z-ai/glm-5.2:free" not in retry_kwargs["extra_body"]["models"]


async def test_a_404_no_endpoint_also_falls_back():
    """OpenRouter answers 404 when no endpoint serves the model at all (e.g.
    none that supports tool use) -- as permanent as a 400 for that model."""
    provider = scripted(_provider(), _model_rejected(404), [_chunk("hi", finish_reason="stop")])
    events = [event async for event in provider.stream(_request())]
    assert _sent_models(provider) == ["z-ai/glm-5.2:free", _FALLBACKS[0]]
    assert events[-1].model == _FALLBACKS[0]


@pytest.mark.parametrize("status_code", [401, 403])
async def test_bad_credentials_are_not_retried_on_another_model(status_code):
    """A bad key fails identically on every model; retrying only doubles the
    time to the same error."""
    provider = scripted(_provider(), status_error(status_code))
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass
    assert _sent_models(provider) == ["z-ai/glm-5.2:free"]


async def test_only_one_fallback_is_tried():
    """A 400 caused by the request itself (not the model) fails on every
    model -- one retry, then the original error surfaces."""
    provider = scripted(_provider(), _model_rejected(), _model_rejected())
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass
    assert _sent_models(provider) == ["z-ai/glm-5.2:free", _FALLBACKS[0]]


async def test_no_retry_once_text_has_reached_the_caller():
    """Switching models mid-answer would splice two models' text into one
    reply the visitor already half-read."""

    provider = scripted(_provider(), [_chunk("Hel"), _model_rejected()])
    seen = []
    with pytest.raises(LLMConfigurationError):
        async for event in provider.stream(_request()):
            seen.append(event.type)
    assert seen == ["message_start", "text_delta"]
    assert _sent_models(provider) == ["z-ai/glm-5.2:free"]


async def test_with_no_other_model_available_the_original_error_surfaces():
    provider = scripted(_provider(["z-ai/glm-5.2:free"]), _model_rejected())
    with pytest.raises(LLMConfigurationError):
        async for _ in provider.stream(_request()):
            pass
    assert _sent_models(provider) == ["z-ai/glm-5.2:free"]
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["extra_body"]["models"] == ["z-ai/glm-5.2:free"]
