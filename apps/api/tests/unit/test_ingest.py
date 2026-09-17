"""Pure-Python behaviour of `app/rag/ingest.py`'s internal helpers, isolated
from the database -- `tests/integration/test_ingest.py` covers the pipeline
end to end; this covers the two branches inside it that a whole-suite diff
would not otherwise catch going missing.
"""

import pytest

from app.rag import ingest as ingest_module

pytestmark = pytest.mark.anyio


def test_truncate_passes_a_short_message_through_unchanged():
    assert ingest_module._truncate("could not parse PDF") == "could not parse PDF"


def test_truncate_appends_a_suffix_once_the_message_exceeds_the_limit():
    """The brief requires "a truncated message in `error`" for a
    pathological exception (e.g. an HTML error page a provider returned as
    its body) -- this is what actually pins that requirement; without it,
    `_truncate`'s truncating branch could be deleted (making it always
    return the message unchanged) with nothing in the suite going red."""
    limit = ingest_module._ERROR_MESSAGE_LIMIT
    long_message = "x" * (limit + 1)

    result = ingest_module._truncate(long_message)

    assert result.endswith("... (truncated)")
    assert len(result) == limit + len("... (truncated)")
    assert result.startswith("x" * limit)


async def test_embed_all_returns_empty_without_ever_calling_the_provider(monkeypatch):
    """A document with no chunks (e.g. a blank upload) must not reach the
    embedding provider at all -- calling it with an empty batch is not a
    case any provider is obliged to handle sensibly. `_embed_all` has no
    explicit early-return for this (an empty `texts` just makes its batch
    loop's `range(0, 0, ...)` empty), so this is pinning emergent behaviour
    rather than a branch -- worth pinning anyway, since it would be easy for
    a future rewrite of the batching loop to lose it silently. Asserted by
    counting calls, not just checking the returned vectors: a version that
    called `provider.embed([])` and got `[]` back would produce the same
    return value here, so only the call count actually distinguishes "never
    called" from "called once, trivially"."""
    calls = {"n": 0}

    class _ShouldNeverBeCalled:
        name = "should-not-be-called"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += 1
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(ingest_module, "get_embedding_provider", lambda: _ShouldNeverBeCalled())

    vectors, embedding_model = await ingest_module._embed_all([])

    assert vectors == []
    assert embedding_model == "should-not-be-called"
    assert calls["n"] == 0
