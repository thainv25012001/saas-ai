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

One more deliberate choice, called out here because it is easy to assume
the opposite: a tool call the model asks for on the step that turns out to
be the *last* one (`max_steps` is then hit right after) is still executed
and its result still surfaces via `AgentToolCallEnd`, exactly like any
other step. Skipping it would mean either yielding `AgentToolCallStart`
with no matching `AgentToolCallEnd` (breaking the "every issued tool_use
gets a matching tool_result" guarantee this module exists to provide) or
silently dropping that the model asked for a tool at all. A caller that
cares whether a side-effecting tool (e.g. `create_lead`, Task 6) ran right
before a step-limit cutoff has everything it needs to say so: the
`AgentToolCallEnd` for that call arrives immediately before the
`AgentStepLimit`.
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
    """One step's `tool_use` blocks, about to be run."""

    calls: list[ToolUseBlock]


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    """One call's outcome, with the id that ties it back to its `tool_use`
    block carried inline -- deliberately not left to be re-derived by
    zipping `AgentToolCallStart.calls` against `AgentToolCallEnd.results`
    by position. That correlation happens to hold today (`asyncio.gather`
    preserves input order, and the results here are built via
    `zip(..., strict=True)`), but a downstream consumer relying on position
    instead of the id has no defence against a future refactor that
    reorders, filters, or sorts either list -- it would silently attribute
    one tool's result to a different tool's card in the UI, with nothing
    that fails loudly."""

    tool_call_id: str
    result: ToolResult


@dataclass(frozen=True, slots=True)
class AgentToolCallEnd:
    results: list[AgentToolResult]


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
        if max_steps < 1:
            # `max_steps` is sourced from `config.max_agent_steps`, a plain
            # database column (Task 7) -- a `0` there is a data-entry
            # mistake, not a caller decision to accept, and left unchecked
            # it would emit `AgentStepLimit` without ever calling the
            # provider: a turn that answers nothing and looks, from the
            # event stream alone, exactly like a real step-limit hit.
            raise ValueError(f"max_steps must be >= 1, got {max_steps}")
        self.provider = provider
        self.registry = registry
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.effort = effort
        self.max_steps = max_steps

    def _resolve_specs(self, tool_names: list[str]) -> list[ToolSpec]:
        """Specs for every distinct name that resolves, in first-seen order,
        silently dropping any that don't.

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

        `dict.fromkeys` also dedupes before resolving: Task 3's schema
        allows an org-scoped tool to shadow a builtin of the same name (its
        resolution order is left to Task 7), and that is exactly the shape
        through which a caller could end up asking for the same name twice.
        A provider tool list with two entries of the same name is invalid
        wire format Anthropic itself rejects with a 400, so it is closed
        here regardless of how shadowing eventually gets resolved.
        """
        specs: list[ToolSpec] = []
        for name in dict.fromkeys(tool_names):
            try:
                specs.extend(self.registry.specs_for([name]))
            except KeyError:
                logger.warning("agent_tool_unknown_name", tool_name=name)
        return specs

    @staticmethod
    def _unique_calls(calls: list[ToolUseBlock], seen: set[str]) -> list[ToolUseBlock]:
        """`calls` with any id already issued this turn dropped, `seen`
        updated in place.

        A provider is supposed to mint a distinct id per `tool_use` block;
        one that does not is a protocol violation, and every layer below
        this one assumes the id is a key. Reproduced before this guard: two
        calls in one step sharing `id="same"` with different arguments wrote
        **both** `message_tool_calls` rows with the *second* call's
        arguments, because `ChatService.send` keys the calls it has seen by
        id and the second overwrote the first -- so the table whose whole
        reason for existing is post-hoc answerability recorded something the
        model never asked. The same id also reached the playground as two
        React keys, and the model as two `ToolResultBlock`s sharing one
        `tool_use_id`, which Anthropic rejects with a 400.

        **Dropped, not disambiguated, and not executed-then-failed.**
        Rewriting the duplicate to a synthetic id would keep both calls
        running, but `create_lead` is a write: a model that emitted the same
        id twice has given no evidence it meant two distinct writes, and
        inventing an id to make a malformed pair executable is how one
        confused turn becomes two real rows. Returning an error result for
        the duplicate instead is no better -- it would still carry the
        colliding id downstream, which is the defect itself. Dropping keeps
        every invariant below intact: one `tool_use` per id in history, one
        matching `tool_result`, one SSE card, one row -- each carrying the
        arguments that actually ran. The dropped call is preserved where it
        belongs, in the log.
        """
        unique: list[ToolUseBlock] = []
        for call in calls:
            if call.id in seen:
                logger.warning(
                    "agent_duplicate_tool_call_id", tool_call_id=call.id, tool_name=call.name
                )
                continue
            seen.add(call.id)
            unique.append(call)
        return unique

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
        # Turn-wide, not per-step: everything downstream correlates a call to
        # its result by id, for the whole turn -- `ChatService.send`'s
        # `tool_calls_by_id`, `message_tool_calls.tool_call_id`, and the
        # playground's React key on `ToolCall`. See `_unique_calls`.
        seen_call_ids: set[str] = set()

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
            if outcome is None:
                # `_run_step` always yields exactly one `_StepOutcome`, as
                # its very last item -- a plain `assert` here would be
                # stripped under `python -O`, turning this into a confusing
                # `AttributeError` on `None` instead of a clear signal that
                # `_run_step` itself is broken.
                raise AssertionError("_run_step ended without yielding a _StepOutcome")

            total_usage = _sum_usage(total_usage, outcome.usage)
            yield AgentUsage(usage=total_usage)

            calls = self._unique_calls(outcome.calls, seen_call_ids)

            assistant_blocks: list[ContentBlock] = []
            if outcome.text:
                assistant_blocks.append(TextBlock(text=outcome.text))
            assistant_blocks.extend(calls)
            if assistant_blocks:
                # Skipped, never appended empty: an assistant turn with no
                # content at all is invalid wire format for Anthropic, and
                # `docs/ARCHITECTURE.md` §5.1's note says the same. Only
                # reachable now that `_unique_calls` can empty a step that
                # produced calls but no text.
                history.append(Message(role="assistant", content=assistant_blocks))

            if not calls:
                # Either the model is done talking, or every call it made
                # this step was a duplicate id (`_unique_calls` logged each
                # one). Both end the turn here: with no `tool_use` block in
                # the assistant message just appended, there is nothing for a
                # further step to respond to.
                break

            yield AgentToolCallStart(calls=calls)
            # Isolation: `return_exceptions=True` means one call's exception
            # (e.g. a bug in the registry itself, not just in a tool body --
            # `ToolRegistry.execute` already turns a tool's own failure into
            # `ToolResult(is_error=True)`, so this is a second, independent
            # safety net) is captured per-call and never aborts its siblings.
            raw_results = await asyncio.gather(
                *(self.registry.execute(call, ctx) for call in calls),
                return_exceptions=True,
            )
            results: list[AgentToolResult] = []
            for call, raw in zip(calls, raw_results, strict=True):
                if isinstance(raw, BaseException):
                    logger.exception("agent_tool_call_raised", tool_name=call.name, exc_info=raw)
                    tool_result = ToolResult(
                        content=f"'{call.name}' failed unexpectedly", is_error=True
                    )
                else:
                    tool_result = raw
                results.append(AgentToolResult(tool_call_id=call.id, result=tool_result))
            yield AgentToolCallEnd(results=results)

            # Every issued tool_use gets a matching tool_result -- read off
            # `tool_call_id` on each `AgentToolResult`, not off position, so
            # a result can never be mismatched to the wrong call's id.
            history.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id=r.tool_call_id,
                            content=r.result.content,
                            is_error=r.result.is_error,
                        )
                        for r in results
                    ],
                )
            )
        else:
            # Reached only if the loop ran `max_steps` times without ever
            # `break`-ing -- i.e. the model asked for tools on every step.
            yield AgentStepLimit(max_steps=self.max_steps)
