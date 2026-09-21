from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import ModelCapabilities, SchemaT
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    ContentBlock,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    ToolUseBlock,
    ToolUseEvent,
    Usage,
    UsageEvent,
)

_DEFAULT_SCRIPT = [
    "Thanks for asking! ",
    "I am a placeholder response ",
    "until a real provider is configured.",
]


@dataclass(frozen=True, slots=True)
class FakeToolCall:
    """One scripted tool call for a `turns`-based `FakeProvider`.

    `id` is optional -- a sequential default (`call_1`, `call_2`, ...) is
    assigned when omitted, since a scripted test usually cares about the
    name and arguments a tool receives, not the literal id later round-
    tripped back on a `ToolResultBlock`.

    The counter runs across the WHOLE scripted conversation, not per turn.
    It used to restart each turn, so a two-step script with one call in each
    step handed both of them `call_1` -- which no real provider does, and
    which `AgentRunner` now (correctly) treats as a duplicate and drops,
    since the accumulated history of a multi-step turn goes to the provider
    as ONE request and Anthropic rejects two `tool_use` blocks sharing an id
    inside it. A fake that can only produce wire format a real provider
    would be 400-ed for is not a useful fake.
    """

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


#: One turn of a scripted conversation: either the model answering in prose,
#: or asking for one or more tools at once (Task 4's loop must run parallel
#: calls, so a turn is a *list* of calls, not just one).
FakeTurn = str | list[FakeToolCall]


class FakeProvider:
    """A deterministic provider with no network calls.

    This is production code, not a test fixture, for two reasons. Every test in this
    phase runs against it, so the suite is fast, free and reproducible for anyone who
    clones the repo without an API key — a suite that only runs for people with billing
    configured stops being run. And it is what `ENVIRONMENT=local` falls back to when no
    key is set, so the playground works out of the box.
    """

    name = "fake"

    def __init__(
        self,
        script: list[str] | None = None,
        turns: list[FakeTurn] | None = None,
        usage: Usage | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        # `turns` is a NEW way to construct this provider, additive to the
        # original `script` form every existing chat test already uses —
        # never a replacement for it. The two answer different questions
        # about "how many chunks arrive": `script` is chunks of ONE turn,
        # all replayed within a single `stream()` call (unchanged below);
        # `turns` is one scripted turn PER `stream()`/`generate()` call, the
        # shape Task 4's agent loop needs to script a tool call followed by
        # a prose answer. Reusing one parameter for both would make
        # `script=["a", "b"]` ambiguous between the two.
        if script is not None and turns is not None:
            raise ValueError("pass either `script` or `turns`, not both")
        if turns is not None and fail_with is not None:
            # `fail_with`'s documented contract ("raises after at least one
            # chunk has streamed") is defined only for the flat `script`
            # chunk list, which has no equivalent granularity inside a
            # scripted turn. Reject the combination outright rather than
            # pick a silent, unspecified interpretation of it.
            raise ValueError("fail_with is not supported together with turns")
        self._script = script if script is not None else list(_DEFAULT_SCRIPT)
        self._turns: deque[FakeTurn] | None = deque(turns) if turns is not None else None
        self._usage = usage or Usage(input_tokens=10, output_tokens=5)
        if fail_with is not None and not self._script:
            # The documented contract is "raises after at least one chunk has
            # streamed" — that is the whole point of `fail_with` (Task 6 needs
            # to reproduce tokens already reaching the browser before the
            # provider dies). An empty script has no chunk to fail after, so
            # this would either silently fail before any output or raise from
            # `generate()` with nothing to distinguish it from any other
            # failure mode. Reject it at construction instead of yielding a
            # contract violation later.
            raise ValueError("fail_with requires a non-empty script")
        self._fail_with = fail_with
        self.last_request: CompletionRequest | None = None
        #: Monotonic across every turn of this instance's script -- see
        #: `FakeToolCall`'s docstring.
        self._calls_issued = 0

    def _next_turn(self) -> FakeTurn:
        assert self._turns is not None
        if not self._turns:
            # A loop bug that calls stream()/generate() more times than the
            # script provides turns must fail loudly here, not silently
            # replay the last turn (which would hide the bug) or hang.
            raise RuntimeError(
                "FakeProvider's scripted turns are exhausted: stream() or "
                "generate() was called more times than `turns` provided"
            )
        return self._turns.popleft()

    def _turn_response(self, turn: FakeTurn, request: CompletionRequest) -> CompletionResponse:
        content: list[ContentBlock]
        if isinstance(turn, str):
            content = [TextBlock(text=turn)]
            stop_reason = "end_turn"
        else:
            content = [self._tool_use_block(call) for call in turn]
            stop_reason = "tool_use"
        return CompletionResponse(
            content=content, usage=self._usage, model=request.model, stop_reason=stop_reason
        )

    def _tool_use_block(self, call: FakeToolCall) -> ToolUseBlock:
        self._calls_issued += 1
        return ToolUseBlock(
            id=call.id or f"call_{self._calls_issued}", name=call.name, input=call.input
        )

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        self.last_request = request
        if self._fail_with is not None:
            raise self._fail_with
        if self._turns is not None:
            # Shares one cursor with `_stream`: whichever entry point is
            # called next consumes the next scripted turn, the same way a
            # caller can never replay a turn against a real provider by
            # picking a different method.
            return self._turn_response(self._next_turn(), request)
        return CompletionResponse(
            content=[TextBlock(text="".join(self._script))],
            usage=self._usage,
            model=request.model,
            stop_reason="end_turn",
        )

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.last_request = request
        yield MessageStartEvent(model=request.model)
        if self._turns is not None:
            turn = self._next_turn()
            if isinstance(turn, str):
                yield TextDeltaEvent(text=turn)
                stop_reason = "end_turn"
            else:
                for call in turn:
                    yield ToolUseEvent(block=self._tool_use_block(call))
                stop_reason = "tool_use"
            yield UsageEvent(usage=self._usage)
            yield MessageEndEvent(stop_reason=stop_reason, usage=self._usage, model=request.model)
            return
        for index, chunk in enumerate(self._script):
            # Fail AFTER the first chunk so callers can reproduce the case where
            # output has already reached the browser before the provider dies.
            if self._fail_with is not None and index == 1:
                raise self._fail_with
            yield TextDeltaEvent(text=chunk)
        if self._fail_with is not None and len(self._script) <= 1:
            raise self._fail_with
        yield UsageEvent(usage=self._usage)
        yield MessageEndEvent(stop_reason="end_turn", usage=self._usage, model=request.model)

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT:
        self.last_request = request
        return schema()
