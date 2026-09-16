"""Stand-ins for the OpenAI SDK's streaming `create(...)`.

Shared because `OpenRouterProvider` subclasses `OpenAIProvider` and is driven
through the same wire format, so both test modules need the same stubs. Keeping
one copy also keeps the `AsyncMock` reasoning below in one place: it is the
non-obvious part, and a second copy would lose it.
"""

from unittest.mock import AsyncMock, MagicMock


def chunk(text=None, usage=None, finish_reason=None):
    """One streamed chunk. `usage` is a `(prompt_tokens, completion_tokens)`
    pair; a chunk with neither text nor a finish reason models the usage-only
    final chunk, which carries an empty `choices`."""
    stub = MagicMock()
    if usage is None:
        stub.usage = None
    else:
        stub.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    if text is None and finish_reason is None:
        stub.choices = []
    else:
        choice = MagicMock()
        choice.delta.content = text
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

    def _aiter(**_kwargs):
        async def gen():
            for item in chunks:
                yield item

        return gen()

    provider._client = MagicMock()  # noqa: SLF001
    provider._client.chat.completions.create = AsyncMock(side_effect=_aiter)  # noqa: SLF001
    return provider
