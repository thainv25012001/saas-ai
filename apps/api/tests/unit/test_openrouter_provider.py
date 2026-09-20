"""OpenRouter speaks OpenAI's wire format, so `OpenRouterProvider` inherits
the whole stream loop and error mapping from `OpenAIProvider` -- which
`test_openai_provider.py` already covers exhaustively. These tests cover only
what is genuinely different: where the requests go, what the provider calls
itself, and one end-to-end smoke test proving the inherited loop really does
run when driven through this subclass.
"""

import pytest

from app.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider
from app.llm.types import CompletionRequest, Message, ToolSpec

from ._llm_stubs import chunk, streaming

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


def _provider_with(chunks) -> OpenRouterProvider:
    return streaming(OpenRouterProvider(api_key="test-key"), chunks)


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
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}
