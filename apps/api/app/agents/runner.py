"""The multi-step agent loop: Task 4 of Phase 4, and the task the whole
phase exists for.

Turns a one-shot "ask the model, stream the answer" into an agent: stream a
step, notice the model asked for tools, run them through `ToolRegistry`,
feed the results back, and go round again until the model stops asking or
`max_steps` is hit. Follows `docs/ARCHITECTURE.md` §5.1's `for ... else`
shape, with one deliberate correction the brief calls out explicitly: usage
is summed across steps here, where §5.1's pseudocode overwrites it (a
multi-step turn would otherwise under-report tokens actually spent).

Scope, per the Task 4 brief: provider-and-registry only. No database
session, no savepoint, no persistence -- those belong to whoever owns the
session (Task 7). This module never imports `sqlalchemy` or a `Session`.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.types import (
    CompletionRequest,
    ContentBlock,
    Effort,
    Message,
    TextBlock,
    TextDeltaEvent,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    ToolUseEvent,
    Usage,
    UsageEvent,
)
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentToolCallStart:
    """One step's `tool_use` blocks, about to be run. `calls[i]` and the
    following `AgentToolCallEnd.results[i]` are the same call, by position
    -- `asyncio.gather` preserves the order of the awaitables it was given,
    so no separate id-keyed mapping is needed here."""

    calls: list[ToolUseBlock]


@dataclass(frozen=True, slots=True)
class AgentToolCallEnd:
    results: list[ToolResult]


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """The RUNNING TOTAL across every step so far, not just the step that
    just finished -- see the module docstring. A consumer that only reads
    the last `AgentUsage` in the stream gets the correct total for the whole
    turn without having to sum anything itself."""

    usage: Usage


@dataclass(frozen=True, slots=True)
class AgentStepLimit:
    """Emitted instead of a silent stop when `max_steps` is exhausted while
    the model was still asking for tools. Carries the limit that was hit so
    a caller logging/surfacing this doesn't have to already know it."""

    max_steps: int


AgentEvent = AgentTextDelta | AgentToolCallStart | AgentToolCallEnd | AgentUsage | AgentStepLimit


def _sum_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


@dataclass(slots=True)
class _StepOutcome:
    text: str = ""
    calls: list[ToolUseBlock] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class AgentRunner:
    """Drives one agent turn to completion against a provider and a tool
    registry. Holds no conversation state between calls to `run` -- each
    call is an independent turn, exactly like `LLMProvider.stream` itself.
    """

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        effort: Effort | None = None,
        max_steps: int,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.effort = effort
        self.max_steps = max_steps

    def _resolve_specs(self, tool_names: list[str]) -> list[ToolSpec]:
        """Specs for every name that resolves, in the order given, silently
        dropping any that don't.

        `ToolRegistry.specs_for` raises `KeyError` on an unregistered name
        by design (see its docstring): a caller asking for a spec that does
        not exist is treated as a misconfiguration to fail loudly on, not a
        model's mistake to paper over. But `tool_names` here is a runtime
        parameter that, from Task 7 onward, is read off `agent_tools` rows
        -- and a stale row naming a since-deleted or renamed tool is a real
        possibility, not a bug in this module. Failing the entire turn (or
        the whole tool list) over one bad row would be strictly worse than
        the model simply not being offered that one tool, so each name is
        resolved independently and a miss is logged and skipped rather than
        propagated. A genuinely empty `tool_names`, or one where every name
        is stale, degrades to a plain no-tools turn -- never an exception.
        """
        specs: list[ToolSpec] = []
        for name in tool_names:
            try:
                specs.extend(self.registry.specs_for([name]))
            except KeyError:
                logger.warning("agent_tool_unknown_name", tool_name=name)
        return specs

    async def _run_step(
        self, request: CompletionRequest
    ) -> AsyncIterator[AgentEvent | _StepOutcome]:
        """Streams one step, yielding `AgentTextDelta` as text arrives, and
        finally yields a `_StepOutcome` summarizing what the step produced.

        Deliberately does not switch on positional indexing into
        `response.content` -- a provider (the fake included, for tool-only
        turns) is not required to emit a filler `TextBlock` alongside tool
        calls, so every block is identified by its event *type*, never by
        position. `MessageEndEvent`/`ErrorEvent` are unhandled here for the
        same reason `ChatService.send` leaves them unhandled: no provider
        today signals failure via an in-band `ErrorEvent`, and this loop's
        "keep going or stop" decision is the presence of `tool_use` blocks,
        never a `stop_reason` string.
        """
        text_parts: list[str] = []
        calls: list[ToolUseBlock] = []
        usage = Usage()
        async for event in self.provider.stream(request):
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.text)
                yield AgentTextDelta(text=event.text)
            elif isinstance(event, ToolUseEvent):
                calls.append(event.block)
            elif isinstance(event, UsageEvent):
                usage = event.usage
        yield _StepOutcome(text="".join(text_parts), calls=calls, usage=usage)

    async def run(
        self,
        system: str,
        messages: list[Message],
        tool_names: list[str],
        ctx: ToolContext,
    ) -> AsyncIterator[AgentEvent]:
        # A local working copy -- the caller's list is never mutated. Each
        # step appends the assistant's turn (text + any tool_use blocks) and,
        # if there were calls, a user-role message carrying the matching
        # tool_result blocks, exactly what the next step's request needs.
        history = list(messages)
        specs = self._resolve_specs(tool_names)
        total_usage = Usage()

        for _step in range(self.max_steps):
            request = CompletionRequest(
                model=self.model,
                messages=history,
                system=system,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                tools=specs or None,
                effort=self.effort,
            )

            outcome: _StepOutcome | None = None
            async for item in self._run_step(request):
                if isinstance(item, _StepOutcome):
                    outcome = item
                else:
                    yield item
            assert outcome is not None  # _run_step always yields exactly one

            total_usage = _sum_usage(total_usage, outcome.usage)
            yield AgentUsage(usage=total_usage)

            assistant_blocks: list[ContentBlock] = []
            if outcome.text:
                assistant_blocks.append(TextBlock(text=outcome.text))
            assistant_blocks.extend(outcome.calls)
            history.append(Message(role="assistant", content=assistant_blocks))

            if not outcome.calls:
                break  # the model is done talking -- no tools requested

            yield AgentToolCallStart(calls=outcome.calls)
            # Isolation: `return_exceptions=True` means one call's exception
            # (e.g. a bug in the registry itself, not just in a tool body --
            # `ToolRegistry.execute` already turns a tool's own failure into
            # `ToolResult(is_error=True)`, so this is a second, independent
            # safety net) is captured per-call and never aborts its siblings.
            raw_results = await asyncio.gather(
                *(self.registry.execute(call, ctx) for call in outcome.calls),
                return_exceptions=True,
            )
            results: list[ToolResult] = []
            for call, raw in zip(outcome.calls, raw_results, strict=True):
                if isinstance(raw, BaseException):
                    logger.exception("agent_tool_call_raised", tool_name=call.name, exc_info=raw)
                    results.append(
                        ToolResult(content=f"'{call.name}' failed unexpectedly", is_error=True)
                    )
                else:
                    results.append(raw)
            yield AgentToolCallEnd(results=results)

            # Every issued tool_use gets a matching tool_result -- built
            # from the same zipped pair above, so a result can never be
            # dropped or mismatched to the wrong call's id.
            history.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id=call.id,
                            content=result.content,
                            is_error=result.is_error,
                        )
                        for call, result in zip(outcome.calls, results, strict=True)
                    ],
                )
            )
        else:
            # Reached only if the loop ran `max_steps` times without ever
            # `break`-ing -- i.e. the model asked for tools on every step.
            yield AgentStepLimit(max_steps=self.max_steps)
