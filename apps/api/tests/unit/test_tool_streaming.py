"""FakeProvider's scriptable multi-turn form, added so Task 4's agent loop
can be driven offline: turn 1 asks for a tool, turn 2 -- after the loop feeds
the tool result back -- answers in prose, with no real provider involved.

Provider-specific fragment accumulation (Anthropic's `input_json_delta`,
OpenAI's index-keyed `tool_calls` deltas) is covered in each provider's own
test module, not here: those are wire-format problems, this is a scripting
contract.
"""

import pytest

from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.llm.types import CompletionRequest, Message

pytestmark = pytest.mark.anyio


def _request() -> CompletionRequest:
    return CompletionRequest(
        model="fake-1",
        messages=[Message.text("user", "hello")],
        system="you are a test",
        max_tokens=100,
    )


async def test_fake_provider_plays_a_two_turn_script_in_order():
    """The shape Task 4's loop depends on. Each `stream()` call advances to
    the next turn; nothing about turn 2 is visible while turn 1 plays."""
    provider = FakeProvider(
        turns=[
            [FakeToolCall(name="lookup_order", input={"order_id": "A1"})],
            "Your order ships tomorrow.",
        ]
    )

    first = [event async for event in provider.stream(_request())]
    assert [e.type for e in first] == ["message_start", "tool_use", "usage", "message_end"]
    assert first[1].block.name == "lookup_order"
    assert first[1].block.input == {"order_id": "A1"}
    assert first[-1].stop_reason == "tool_use"

    second = [event async for event in provider.stream(_request())]
    assert [e.type for e in second] == ["message_start", "text_delta", "usage", "message_end"]
    assert second[1].text == "Your order ships tomorrow."
    assert second[-1].stop_reason == "end_turn"


async def test_fake_provider_turn_with_two_concurrent_tool_calls():
    """Task 4's loop must run parallel tool calls, not just one at a time --
    each call needs its own id so a `ToolResultBlock` can be matched back to
    the right one."""
    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(name="search", input={"q": "a"}),
                FakeToolCall(name="search", input={"q": "b"}),
            ]
        ]
    )
    events = [event async for event in provider.stream(_request())]
    tool_events = [e for e in events if e.type == "tool_use"]
    assert [e.block.input for e in tool_events] == [{"q": "a"}, {"q": "b"}]
    assert len({e.block.id for e in tool_events}) == 2


async def test_fake_provider_raises_once_turns_are_exhausted():
    """A loop bug that calls `stream()` more times than the script provides
    turns must fail loudly, not silently replay the last turn or hang."""
    provider = FakeProvider(turns=["only turn"])
    async for _ in provider.stream(_request()):
        pass
    with pytest.raises(RuntimeError):
        async for _ in provider.stream(_request()):
            pass


def test_turns_and_script_are_mutually_exclusive():
    with pytest.raises(ValueError):
        FakeProvider(script=["x"], turns=["y"])


def test_fail_with_is_rejected_alongside_turns():
    """`fail_with`'s documented contract ("raises after at least one chunk
    has streamed") is defined only for the flat `script` chunk list, which
    has no equivalent inside a scripted turn. Reject the combination outright
    rather than pick a silent, unspecified interpretation of it."""
    with pytest.raises(ValueError):
        FakeProvider(turns=["x"], fail_with=LLMUnavailableError("gone"))


async def test_generate_also_advances_the_turn_cursor():
    """`generate()` and `stream()` share one cursor -- calling either one
    consumes the next scripted turn, the same way a real provider call does
    (a caller never gets to replay a turn by picking a different method)."""
    provider = FakeProvider(turns=[[FakeToolCall(name="search", input={"q": "x"})], "done"])
    response = await provider.generate(_request())
    assert response.stop_reason == "tool_use"
    assert response.content[0].name == "search"
    second = await provider.generate(_request())
    assert second.text == "done"
    assert second.stop_reason == "end_turn"


async def test_old_script_construction_is_completely_unaffected():
    """Pin against the exact regression this task must not cause: every
    existing chat test constructs `FakeProvider(script=[...])` and expects
    every chunk in ONE `stream()` call, not one chunk per call."""
    provider = FakeProvider(script=["Hel", "lo!"])
    types_seen = [event.type async for event in provider.stream(_request())]
    assert types_seen == ["message_start", "text_delta", "text_delta", "usage", "message_end"]
