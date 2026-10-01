"""Stand-ins for the OpenAI SDK's streaming `create(...)`.

Shared because `OpenRouterProvider` subclasses `OpenAIProvider` and is driven
through the same wire format, so both test modules need the same stubs. Keeping
one copy also keeps the `AsyncMock` reasoning below in one place: it is the
non-obvious part, and a second copy would lose it.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai


def tool_call_delta(index, id=None, name=None, arguments=None):
    """One fragment of a streamed OpenAI tool call.

    A real delta carries `id` and `function.name` only on the FIRST fragment
    for a given `index`; every later fragment for that same call carries
    `id=None`, `function.name=None`, and only a piece of `function.arguments`
    -- `id`/`name` default to `None` here for exactly that reason, not as a
    generic convenience default.

    `MagicMock(name=...)` cannot be used for `function.name`: the mock
    constructor special-cases the `name` kwarg as the mock's own repr, not an
    attribute, so it is set after construction instead.
    """
    delta = MagicMock()
    delta.index = index
    delta.id = id
    if name is None and arguments is None:
        delta.function = None
    else:
        function = MagicMock()
        function.name = name
        function.arguments = arguments
        delta.function = function
    return delta


def chunk(text=None, usage=None, finish_reason=None, tool_calls=None):
    """One streamed chunk. `usage` is a `(prompt_tokens, completion_tokens)`
    pair; a chunk with no text, finish reason, or tool calls models the
    usage-only final chunk, which carries an empty `choices`."""
    stub = MagicMock()
    if usage is None:
        stub.usage = None
    else:
        stub.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    if text is None and finish_reason is None and tool_calls is None:
        stub.choices = []
    else:
        choice = MagicMock()
        choice.delta.content = text
        choice.delta.tool_calls = tool_calls
        choice.finish_reason = finish_reason
        stub.choices = [choice]
    return stub


def streaming(provider, chunks):
    """Point `provider` at a stream of `chunks` and hand it back.

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

    return _install(provider, lambda **_kwargs: _aiter(chunks))


def scripted(provider, *outcomes):
    """Point `provider` at one outcome per `create(...)` call, in order.

    An exception outcome is raised by the call itself (the request was
    refused); a list is streamed, and an exception INSIDE the list is raised
    at that point in the stream (the request failed mid-answer).
    """
    remaining = list(outcomes)

    def _create(**_kwargs):
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _aiter(outcome)

    return _install(provider, _create)


def status_error(status_code, body=None):
    """The SDK's `APIStatusError` for an HTTP `status_code` response. The
    error mapping reads only the status, so the URL is immaterial."""
    request = httpx2.Request("POST", "https://api.openai.com/v1")
    response = httpx2.Response(
        status_code, request=request, json=body or {"error": {"type": "some_error"}}
    )
    return openai.APIStatusError("failed", response=response, body=body)


def _aiter(items):
    async def gen():
        for item in items:
            if isinstance(item, BaseException):
                raise item
            yield item

    return gen()


def _install(provider, create):
    provider._client = MagicMock()  # noqa: SLF001
    provider._client.chat.completions.create = AsyncMock(side_effect=create)  # noqa: SLF001
    return provider
