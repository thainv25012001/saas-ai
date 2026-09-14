import pytest

from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider
from app.llm.types import CompletionRequest, Message, Usage

pytestmark = pytest.mark.anyio


def _request() -> CompletionRequest:
    return CompletionRequest(
        model="fake-1",
        messages=[Message.text("user", "hello")],
        system="you are a test",
        max_tokens=100,
    )


async def test_stream_emits_start_deltas_and_end_in_order():
    provider = FakeProvider(script=["Hel", "lo!"])
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_stream_text_reassembles_to_the_script():
    provider = FakeProvider(script=["Hel", "lo!"])
    # A bare async generator expression passed to a sync `join()` is never
    # materialized (it stays an async generator object); a list comprehension
    # with `async for`, by contrast, is awaited inline. See PEP 530.
    text = "".join(
        [event.text async for event in provider.stream(_request()) if event.type == "text_delta"]
    )
    assert text == "Hello!"


async def test_generate_returns_the_same_text_as_the_stream():
    """The two entry points must not drift — callers choose between them freely."""
    provider = FakeProvider(script=["a", "b"])
    response = await provider.generate(_request())
    assert response.text == "ab"


async def test_reported_usage_is_what_was_configured():
    provider = FakeProvider(script=["x"], usage=Usage(input_tokens=7, output_tokens=3))
    response = await provider.generate(_request())
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3


async def test_fail_with_raises_after_some_output_has_streamed():
    """Mid-stream failure is the awkward case the chat service must handle: some
    tokens already reached the browser. This is how we reproduce it."""
    provider = FakeProvider(script=["partial ", "more"], fail_with=LLMUnavailableError("gone"))
    seen: list[str] = []
    with pytest.raises(LLMUnavailableError):
        async for event in provider.stream(_request()):
            if event.type == "text_delta":
                seen.append(event.text)
    assert seen == ["partial "]


async def test_capabilities_are_permissive():
    provider = FakeProvider()
    caps = provider.capabilities("fake-1")
    assert caps.supports_sampling is True


async def test_last_request_is_recorded_for_assertions():
    """Tests need to assert on what the caller sent — e.g. that the system prompt
    was rendered from the active prompt version."""
    provider = FakeProvider(script=["x"])
    await provider.generate(_request())
    assert provider.last_request is not None
    assert provider.last_request.system == "you are a test"
