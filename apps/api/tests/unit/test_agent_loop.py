"""`app/agents/runner.py`: the multi-step loop that turns a one-shot answerer
into an agent -- streams a step, notices `tool_use` blocks, runs them through
`ToolRegistry`, feeds the results back, and goes round again until the model
stops asking for tools or `max_steps` is hit.

Per `docs/ARCHITECTURE.md` §5.1/§5.2 and the Task 4 brief, three properties
are load-bearing and each test below is built to fail if that property
breaks, not merely because a scripted fake said so:

- **Termination**: `for ... else` -- the `else` (`AgentStepLimit`) fires only
  when the loop was never `break`-ed out of.
- **Isolation**: calls within one step run concurrently, one failure must not
  abort its siblings, and every issued `tool_use` gets a matching
  `tool_result`.
- **Failure is not fiction**: a failed tool reaches the model as a readable
  error, never swallowed into an empty success.

Plus: usage sums across steps rather than being overwritten by the last one.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from app.agents.runner import (
    AgentRunner,
    AgentStepLimit,
    AgentTextDelta,
    AgentToolCallEnd,
    AgentToolCallStart,
    AgentUsage,
)
from app.llm.base import LLMProvider, ModelCapabilities
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseEvent,
    Usage,
    UsageEvent,
)
from app.tools.base import AgentTool, ToolContext, ToolResult
from app.tools.registry import ToolRegistry

pytestmark = pytest.mark.anyio


def _ctx() -> ToolContext:
    return ToolContext(
        organization_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        request_id="req-1",
    )


class _NoArgs(BaseModel):
    pass


class _SearchArgs(BaseModel):
    q: str


class _SearchTool(AgentTool):
    """Returns a result keyed off its own argument so two calls in the same
    step can be told apart -- a test that used the same canned string for
    both could pass even if the loop mixed the results up."""

    name = "search"
    description = "Searches for something."
    args_model = _SearchArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, _SearchArgs)
        return ToolResult(content=f"result for {args.q}", data={"q": args.q})


class _BoomTool(AgentTool):
    name = "boom"
    description = "Always raises, as if a downstream dependency broke."
    args_model = _NoArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("kaboom")


def _registry(*tools: AgentTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _runner(provider: LLMProvider, registry: ToolRegistry, *, max_steps: int = 5) -> AgentRunner:
    return AgentRunner(
        provider,
        registry,
        model="fake-1",
        max_tokens=100,
        max_steps=max_steps,
    )


async def test_no_tool_calls_is_one_step_with_text_streamed() -> None:
    provider = FakeProvider(turns=["Hello there."])
    runner = _runner(provider, _registry())

    events = [
        event async for event in runner.run("system", [Message.text("user", "hi")], [], _ctx())
    ]

    text_events = [e for e in events if isinstance(e, AgentTextDelta)]
    assert "".join(e.text for e in text_events) == "Hello there."
    assert not any(isinstance(e, AgentToolCallStart) for e in events)
    assert not any(isinstance(e, AgentStepLimit) for e in events)
    # Only one call was ever made to the provider -- pins "one step".
    assert provider._turns is not None
    assert len(provider._turns) == 0


async def test_one_tool_call_then_text_is_two_steps_and_the_result_reaches_step_two() -> None:
    """The load-bearing assertion is on the CAPTURED request handed to the
    provider on step two, not on the registry's call count -- a loop that
    forgot to append the tool result to history could still call the
    registry once and pass a weaker test."""
    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="search", input={"q": "shoes"})],
            "Found some shoes for you.",
        ]
    )
    runner = _runner(provider, _registry(_SearchTool()))

    events = [
        event
        async for event in runner.run(
            "system", [Message.text("user", "find shoes")], ["search"], _ctx()
        )
    ]

    # Two steps happened: the fake's scripted turns queue is now empty.
    assert provider._turns is not None
    assert len(provider._turns) == 0

    text = "".join(e.text for e in events if isinstance(e, AgentTextDelta))
    assert text == "Found some shoes for you."

    # Inspect what step two was actually handed, not merely that *a* call
    # happened -- a mutated loop that fed back an empty tool result, or the
    # wrong tool_use_id, would still pass a weaker assertion.
    second_request = provider.last_request
    assert second_request is not None
    tool_result_blocks = [
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(tool_result_blocks) == 1
    assert tool_result_blocks[0].tool_use_id == "call_1"
    assert tool_result_blocks[0].content == "result for shoes"
    assert tool_result_blocks[0].is_error is False

    # The assistant's tool_use block from step one is also carried into step
    # two's history -- real providers reject a tool_result with no matching
    # preceding tool_use.
    tool_use_blocks = [
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0].id == "call_1"


async def test_two_parallel_calls_both_return_distinct_results() -> None:
    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(id="c1", name="search", input={"q": "a"}),
                FakeToolCall(id="c2", name="search", input={"q": "b"}),
            ],
            "done",
        ]
    )
    runner = _runner(provider, _registry(_SearchTool()))

    events = [event async for event in runner.run("system", [], ["search"], _ctx())]

    [end] = [e for e in events if isinstance(e, AgentToolCallEnd)]
    assert len(end.results) == 2
    contents = {r.result.content for r in end.results}
    assert contents == {"result for a", "result for b"}
    assert all(not r.result.is_error for r in end.results)
    # Each result carries the id of the call it belongs to, inline -- a
    # consumer must not have to zip this list against `AgentToolCallStart`
    # by position to know which call produced which result.
    by_id = {r.tool_call_id: r.result.content for r in end.results}
    assert by_id == {"c1": "result for a", "c2": "result for b"}


async def test_one_of_two_parallel_calls_raising_does_not_abort_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the runner's OWN isolation (`asyncio.gather(...,
    return_exceptions=True)`), not the registry's -- `ToolRegistry.execute`
    already catches everything a tool's `execute` raises, so to prove the
    loop does not rely solely on that, `registry.execute` itself is patched
    to raise for one specific call, bypassing the registry's own safety net
    entirely."""
    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(id="c1", name="search", input={"q": "ok"}),
                FakeToolCall(id="c2", name="boom", input={}),
            ],
            "done",
        ]
    )
    registry = _registry(_SearchTool(), _BoomTool())
    real_execute = registry.execute

    async def _flaky_execute(call: ToolUseBlock, ctx: ToolContext) -> ToolResult:
        if call.name == "boom":
            raise RuntimeError("registry itself blew up")
        return await real_execute(call, ctx)

    monkeypatch.setattr(registry, "execute", _flaky_execute)
    runner = _runner(provider, registry)

    events = [event async for event in runner.run("system", [], ["search", "boom"], _ctx())]

    [end] = [e for e in events if isinstance(e, AgentToolCallEnd)]
    assert len(end.results) == 2
    ok_results = [r for r in end.results if not r.result.is_error]
    error_results = [r for r in end.results if r.result.is_error]
    assert len(ok_results) == 1
    assert ok_results[0].tool_call_id == "c1"
    assert ok_results[0].result.content == "result for ok"
    assert len(error_results) == 1
    assert error_results[0].tool_call_id == "c2"

    # Both calls produced a tool_result block fed back to the model -- a
    # missing one is a protocol error real providers reject outright.
    second_request = provider.last_request
    assert second_request is not None
    tool_result_ids = {
        block.tool_use_id
        for message in second_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    assert tool_result_ids == {"c1", "c2"}
    error_blocks = [
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.tool_use_id == "c2"
    ]
    assert error_blocks[0].is_error is True
    assert error_blocks[0].content  # not swallowed into an empty result


async def test_a_model_that_always_asks_for_a_tool_hits_the_step_limit() -> None:
    turns: list[Any] = [
        [FakeToolCall(id=f"c{i}", name="search", input={"q": str(i)})] for i in range(5)
    ]
    provider = FakeProvider(turns=turns)
    runner = _runner(provider, _registry(_SearchTool()), max_steps=5)

    events = [event async for event in runner.run("system", [], ["search"], _ctx())]

    step_limit_events = [e for e in events if isinstance(e, AgentStepLimit)]
    assert len(step_limit_events) == 1
    assert step_limit_events[0].max_steps == 5
    # All 5 scripted steps ran (not fewer, not looping past the cap).
    assert provider._turns is not None
    assert len(provider._turns) == 0


async def test_usage_sums_across_steps_rather_than_being_overwritten() -> None:
    provider = FakeProvider(
        usage=Usage(input_tokens=10, output_tokens=5),
        turns=[
            [FakeToolCall(id="c1", name="search", input={"q": "x"})],
            "done",
        ],
    )
    runner = _runner(provider, _registry(_SearchTool()))

    events = [event async for event in runner.run("system", [], ["search"], _ctx())]

    usage_events = [e for e in events if isinstance(e, AgentUsage)]
    assert len(usage_events) == 2
    final = usage_events[-1].usage
    # Two steps of input=10/output=5 each: an implementation that overwrites
    # instead of summing would report 10/5 here, not 20/10.
    assert final.input_tokens == 20
    assert final.output_tokens == 10


class _FillerThenToolProvider:
    """A minimal `LLMProvider` test double whose first `stream()` call
    emits BOTH filler text and a `tool_use` block in the same step -- a
    shape `FakeProvider`'s `turns=` form cannot script (a scripted turn is
    prose OR calls, never both), but one real providers routinely produce
    ("Let me check that for you," followed by the call). Exists solely to
    make step one's text non-empty, so the claim under test -- that it is
    carried into step two's history rather than dropped -- is falsifiable.
    `FakeProvider`'s tool-only turns can't distinguish "text correctly
    carried through" from "there was never any text to lose" in the first
    place, since `outcome.text` is already `""` in every other test.
    """

    name = "filler-then-tool"

    def __init__(self) -> None:
        self.last_request: CompletionRequest | None = None
        self._step = 0

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        raise NotImplementedError("AgentRunner only calls stream()")

    async def generate_structured(self, request: CompletionRequest, schema: Any) -> Any:
        raise NotImplementedError("AgentRunner only calls stream()")

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.last_request = request
        step, self._step = self._step, self._step + 1
        return self._stream(step)

    async def _stream(self, step: int) -> AsyncIterator[StreamEvent]:
        usage = Usage(input_tokens=1, output_tokens=1)
        yield MessageStartEvent(model=self.name)
        if step == 0:
            yield TextDeltaEvent(text="Let me check that for you.")
            yield ToolUseEvent(block=ToolUseBlock(id="call_1", name="search", input={"q": "shoes"}))
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason="tool_use", usage=usage, model=self.name)
        else:
            yield TextDeltaEvent(text="Found some shoes for you.")
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason="end_turn", usage=usage, model=self.name)


async def test_step_ones_text_alongside_a_tool_call_reaches_step_twos_history() -> None:
    """§5.1's pseudocode only ever appends `tool_call` blocks to the
    assistant message it builds (`blocks.append(ev.block)` runs solely on
    that branch), so read literally, a step that both talks *and* calls a
    tool has its text silently dropped from history -- and on the final
    step, where `blocks` holds no tool calls either, that reduces to an
    *empty* assistant message appended (and, per the spec, persisted). This
    pins the untested half of that deviation: replacing `if outcome.text:`
    with `if False:` must redden this test, not just be true by
    construction."""
    provider = _FillerThenToolProvider()
    runner = _runner(provider, _registry(_SearchTool()))

    async for _ in runner.run("system", [], ["search"], _ctx()):
        pass

    second_request = provider.last_request
    assert second_request is not None
    text_blocks = [
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, TextBlock)
    ]
    assert len(text_blocks) == 1
    assert text_blocks[0].text == "Let me check that for you."


async def test_duplicate_tool_names_are_deduped_before_reaching_the_provider() -> None:
    """Two `agent_tools` rows resolving to the same name (an org-scoped
    tool shadowing a builtin, Task 3's schema leaves that legal) must not
    reach the provider as two identically-named tool specs -- Anthropic
    rejects that with a 400."""
    provider = FakeProvider(turns=["no tools needed"])
    runner = _runner(provider, _registry(_SearchTool()))

    async for _ in runner.run("system", [], ["search", "search"], _ctx()):
        pass

    request = provider.last_request
    assert request is not None
    assert request.tools is not None
    assert [t.name for t in request.tools] == ["search"]


def test_max_steps_below_one_is_rejected() -> None:
    """`max_steps` is read off a database column (Task 7); a `0` there must
    fail loudly at construction, not silently emit `AgentStepLimit` without
    ever calling the provider -- indistinguishable, from the event stream
    alone, from a real step-limit hit on a turn that actually ran."""
    registry = _registry(_SearchTool())
    with pytest.raises(ValueError, match="max_steps"):
        AgentRunner(
            FakeProvider(turns=["unused"]),
            registry,
            model="fake-1",
            max_tokens=100,
            max_steps=0,
        )


async def test_an_unregistered_tool_name_is_skipped_not_a_crash() -> None:
    """`ToolRegistry.specs_for` raises `KeyError` on an unregistered name --
    deliberately, for a caller's own misconfiguration. `tool_names` here
    comes from `agent_tools` DB rows (Task 7), where a stale row naming a
    since-deleted tool is possible; the runner must not let one bad row take
    down the agent's entire tool list."""
    provider = FakeProvider(turns=["fine, no tools needed"])
    runner = _runner(provider, _registry(_SearchTool()))

    events = [
        event async for event in runner.run("system", [], ["search", "does-not-exist"], _ctx())
    ]

    text = "".join(e.text for e in events if isinstance(e, AgentTextDelta))
    assert text == "fine, no tools needed"
    request = provider.last_request
    assert request is not None
    assert request.tools is not None
    assert [t.name for t in request.tools] == ["search"]


async def test_two_calls_in_one_step_sharing_an_id_run_once_and_history_stays_valid() -> None:
    """Whole-branch review, Important 2. Everything below this loop keys a
    call by its id -- `ChatService.send`'s `tool_calls_by_id`, the
    `message_tool_calls` row, the playground's React key -- and a provider
    that reuses one silently corrupts all three: both persisted rows took
    the SECOND call's arguments, and the model received two
    `ToolResultBlock`s sharing one `tool_use_id`, which Anthropic 400s.

    Asserted on the request handed to the provider on step two, not on a
    call count: the history that reaches the model must contain exactly one
    `tool_use` for the id and exactly one matching `tool_result`, and the
    surviving pair must be the FIRST call's -- the one the model asked for
    first, with its own arguments intact.
    """
    provider = FakeProvider(
        turns=[
            [
                FakeToolCall(id="same", name="search", input={"q": "first"}),
                FakeToolCall(id="same", name="search", input={"q": "second"}),
            ],
            "Done.",
        ]
    )
    runner = _runner(provider, _registry(_SearchTool()))

    events = [
        event
        async for event in runner.run("system", [Message.text("user", "hi")], ["search"], _ctx())
    ]

    start = next(e for e in events if isinstance(e, AgentToolCallStart))
    assert [c.input["q"] for c in start.calls] == ["first"]
    end = next(e for e in events if isinstance(e, AgentToolCallEnd))
    assert [r.tool_call_id for r in end.results] == ["same"]
    assert end.results[0].result.content == "result for first"

    request = provider.last_request
    assert request is not None
    assistant = request.messages[-2]
    tool_uses = [b for b in assistant.content if isinstance(b, ToolUseBlock)]
    assert [b.id for b in tool_uses] == ["same"]
    tool_results = [b for b in request.messages[-1].content if isinstance(b, ToolResultBlock)]
    assert [b.tool_use_id for b in tool_results] == ["same"]


async def test_an_id_reused_on_a_later_step_is_dropped_too() -> None:
    """The guard is turn-wide, not per-step: `ChatService.send` accumulates
    `tool_calls_by_id` across every step of the turn, and a `message` has one
    set of `message_tool_calls` rows for the whole turn -- so an id reused
    two steps apart collides exactly as hard as one reused within a step."""
    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="dup", name="search", input={"q": "first"})],
            [FakeToolCall(id="dup", name="search", input={"q": "later"})],
            "Done.",
        ]
    )
    runner = _runner(provider, _registry(_SearchTool()))

    events = [
        event
        async for event in runner.run("system", [Message.text("user", "hi")], ["search"], _ctx())
    ]

    starts = [e for e in events if isinstance(e, AgentToolCallStart)]
    assert len(starts) == 1
    assert [c.input["q"] for c in starts[0].calls] == ["first"]
    # The second step produced nothing executable, so the turn ends there
    # rather than looping on an assistant message with no tool_use in it.
    assert not any(isinstance(e, AgentStepLimit) for e in events)
