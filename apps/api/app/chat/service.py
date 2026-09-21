import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import (
    AgentRunner,
    AgentStepLimit,
    AgentTextDelta,
    AgentToolCallEnd,
    AgentToolCallStart,
    AgentUsage,
)
from app.agents.service import AgentService
from app.conversations.schemas import AppendMessageInput, CreateConversationInput, RecordUsageInput
from app.conversations.service import ConversationService
from app.core.errors import AppError, NotFoundError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.db.models import (
    Agent,
    AgentToolLink,
    ConversationChannel,
    ConversationMessage,
    MessageCitation,
    MessageRole,
    MessageToolCall,
    Organization,
    Tool,
    ToolType,
    UsageKind,
)
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.registry import get_provider
from app.llm.types import Message as LLMMessage
from app.llm.types import ToolUseBlock, Usage
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.service import PromptService
from app.rag.retrieve import CitationPayload
from app.rag.retrieve import excerpt as excerpt_text
from app.tools.base import AgentTool, ToolContext, ToolResult
from app.tools.leads import CreateLeadTool
from app.tools.registry import ToolRegistry
from app.tools.retrieve import RetrieveKnowledgeTool

logger = get_logger(__name__)

# `tool_call_end` deliberately does not carry a tool's full result payload
# on the wire, the same reason `ChatCitations` carries an excerpt rather
# than a full chunk -- a tool result can be a multi-kilobyte assembled
# context block (`assemble_context`'s whole output, for
# `retrieve_knowledge`), and shipping that twice per turn (once as the
# model's own context, once again over SSE for a UI card) buys nothing the
# UI needs beyond "did it work, and roughly what came back". Reuses
# `app/rag/retrieve.py`'s own public `EXCERPT_MAX_CHARS`/`excerpt` directly
# (imported above, as `excerpt_text` -- the bare name would be easy to
# mistake for a field access among the many `.excerpt` references in this
# module) rather than a second, byte-identical copy: both operate on a
# plain `str` (one truncates `ToolResult.content`, the other
# `RetrievedChunk.content`) and there is no reason for the bound or the
# truncation shape to ever drift between the two call sites.

# PHASE-2.md §6: "the last `history_window` turns (config, default 20)".
# There is no dedicated schema column for this yet (see `agent_configs` in
# ARCHITECTURE.md §3.2) -- Phase 2 has no UI to set one -- so the knob lives
# on the service that consumes it, exactly like `provider_override` on the
# same class already does for a value that otherwise resolves from data.
DEFAULT_HISTORY_WINDOW = 20

# `_LockedSessionTool`'s outer, registry-enforced budget (see its
# docstring): a soft, deliberately generous allowance for a call to sit
# queued behind however many sibling calls one step happens to gather,
# on top of the tool's own real timeout -- not a precise worst case, since
# nothing bounds how many calls one step can contain. A call still queued
# past its own budget plus this much is symptomatic of something
# structurally wrong (a leaked lock, a hung sibling that somehow evaded
# its own inner timeout), which is exactly the case this outer bound
# exists to still catch.
_LOCK_WAIT_BUDGET_SECONDS = 30.0

# Every builtin `_build_registry` knows how to construct, keyed implicitly by
# each class's own `name`. A tuple, not a dict, so the class stays the single
# source of the name it registers under -- the same name
# `_resolve_enabled_tool_names` reads out of `tools.name`, and the same one
# `app/db/builtin_tools.py` seeds. A `tools` row naming something not listed
# here resolves to no Python class and is skipped (logged by
# `AgentRunner._resolve_specs`), exactly as a stale row always was.
_BUILTIN_TOOL_CLASSES: tuple[type[AgentTool], ...] = (RetrieveKnowledgeTool, CreateLeadTool)

# How much longer than a tool's OWN budget the event-loop net in
# `_LockedSessionTool._run_bounded` is allowed to run. Deliberately small,
# and deliberately NOT the thing that bounds database work -- see that
# method's docstring: `SET LOCAL statement_timeout` is what stops a slow
# query, so by the time this fires the tool is provably not blocked in a
# statement, which is exactly the condition under which cancelling it is
# safe for the caller's session.
_NON_DB_GRACE_SECONDS = 5.0

#: Postgres SQLSTATE `query_canceled` -- what `statement_timeout` raises, and
#: what `_is_statement_timeout` recognises so an overrun still reads to the
#: model as a timeout rather than as an unexplained failure.
_STATEMENT_TIMEOUT_SQLSTATE = "57014"

# Yielded in place of a `ChatMessageEnd` when the turn's own persistence
# fails. Deliberately generic, and deliberately the same shape every other
# in-band failure uses: the user has already seen the answer stream, and the
# only honest thing left to say is that it was not saved. The real exception
# goes to the log, never to the wire.
_PERSISTENCE_FAILED_MESSAGE = (
    "The assistant answered, but this turn could not be saved. Please try again."
)


def _tool_log_context(ctx: ToolContext) -> dict[str, str]:
    """Correlation fields every tool-layer log line carries (whole-branch
    review, Minor 7). `tool_name` alone cannot tell an operator watching
    `tool_call_invalid_args` spike which tenant or which conversation it is
    happening in, though `ToolContext` has carried all three since Task 1.
    Matches the dialect `app/chat/service.py`'s own `agent_step_limit_reached`
    already uses: stringified ids, not UUID objects."""
    return {
        "organization_id": str(ctx.organization_id),
        "agent_id": str(ctx.agent_id),
        "conversation_id": str(ctx.conversation_id),
        "request_id": ctx.request_id,
    }


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """Whether `exc` is Postgres cancelling a statement that outran
    `statement_timeout`, as opposed to any other DBAPI-level failure.

    Reads `sqlstate` off the driver's own exception (asyncpg's
    `QueryCanceledError` carries it) rather than matching on the message
    text, and via `getattr` rather than an `isinstance` check against
    `asyncpg.exceptions.QueryCanceledError`, so this module does not have to
    import the driver to recognise a condition the SQL standard already
    names.
    """
    return getattr(exc.orig, "sqlstate", None) == _STATEMENT_TIMEOUT_SQLSTATE


_VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render_template(template: str, variables: Mapping[str, str]) -> str:
    """Explicit substitution over declared variables only.

    Deliberately not `str.format`: prompt text is user-authored, and a stray
    `{` in it (a JSON example embedded in the prompt, say) would raise
    `KeyError`/`IndexError` from `format` on every message the agent ever
    sends. Deliberately not an unsandboxed Jinja `Environment` either: prompt
    text is data, not code, and Jinja's default environment would happily
    evaluate attribute access and filters typed into it.

    A placeholder with no matching variable is left untouched rather than
    raising -- an author can reference a variable that is not one of the
    ones this call happens to supply without breaking every request.
    """

    def _substitute(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _VARIABLE_PATTERN.sub(_substitute, template)


@dataclass(frozen=True, slots=True)
class ChatMessageStart:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    #: Whether this turn is the one that created the conversation, rather than
    #: continuing one. Carried here because this is already the event that
    #: announces which conversation the turn belongs to, and the caller needs
    #: it to decide whether to ask for a title. Defaulted, so nothing that
    #: constructs a `ChatMessageStart` without it has to change.
    created: bool = False


@dataclass(frozen=True, slots=True)
class ChatCitations:
    """Grounding sources, as soon as they are known.

    Phase 3 pinned this event to arrive after `ChatMessageStart` and before
    the first `ChatTextDelta`, back when retrieval ran unconditionally as a
    prefix step before the model was ever called. Phase 4 makes retrieval a
    *tool* the model chooses to call, possibly after already emitting text
    (e.g. "Let me check that for you.") -- so that ordering is no longer
    achievable, or even meaningful. The guarantee this event now carries is
    weaker but still real: citations are emitted as soon as the tool call
    that produced them completes (see `ChatService.send`'s
    `AgentToolCallEnd` handling), and always before `ChatMessageEnd` -- so a
    UI can render sources once they exist without waiting for the whole
    answer to finish streaming.

    A single turn may emit more than one of these (each `retrieve_knowledge`
    call that finds anything gets its own), unlike Phase 3's exactly-one
    event.
    """

    citations: list[CitationPayload]


@dataclass(frozen=True, slots=True)
class ChatTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ChatToolCall:
    """One call's request half, as shown to the client -- id, name and the
    arguments the model supplied. Unlike `ChatToolCallResult` below, the
    request side carries nothing that needs excerpting: `arguments` is
    already bounded by the tool's own `args_model` schema, not an
    unstructured blob a tool might return."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatToolCallStart:
    calls: list[ChatToolCall]


@dataclass(frozen=True, slots=True)
class ChatToolCallResult:
    """One call's outcome, compact. `result` is an excerpt of
    `ToolResult.content` (see `excerpt_text`, imported from
    `app.rag.retrieve.excerpt`), never the full payload -- the
    same reason `ChatCitations`' `excerpt` field exists: a tool result can be
    an entire assembled context block, and the UI needs only enough to show
    the call succeeded and roughly what it returned, not a second copy of
    everything the model was handed.
    """

    tool_call_id: str
    tool_name: str
    result: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class ChatToolCallEnd:
    results: list[ChatToolCallResult]


@dataclass(frozen=True, slots=True)
class ChatMessageEnd:
    usage: Usage
    cost_usd: Decimal | None
    latency_ms: int
    model: str
    prompt_version_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ChatError:
    code: str
    message: str


ChatEvent = (
    ChatMessageStart
    | ChatCitations
    | ChatTextDelta
    | ChatToolCallStart
    | ChatToolCallEnd
    | ChatMessageEnd
    | ChatError
)


#: `MessageToolCall.tool_call_id` and `.tool_name` are both `varchar(100)`
#: (`app/db/models/tool.py`). Both are model-derived -- the provider mints the
#: id, the model picks the name -- so neither is bounded by anything this
#: codebase controls.
_TOOL_CALL_FIELD_MAX_CHARS = 100


def _strip_nulls(value: Any) -> Any:
    r"""Remove NUL (byte 0) from every string reachable in `value`.

    Postgres rejects NUL in `text` AND inside a `jsonb` document -- it is not
    a representable character in either, and asyncpg surfaces the refusal as
    a `DBAPIError` at flush time, i.e. long after the answer has streamed.
    Tool arguments come from model output, so a NUL escape inside a JSON
    string argument is a thing a model can simply emit; a tool result's
    content can inherit one from the document it was assembled from.

    Recursive over dicts and lists because `arguments` is arbitrary JSON
    shaped by each tool's own `args_model`, not a flat mapping -- a NUL
    nested three levels down is rejected exactly as hard as one at the top.
    Keys are cleaned as well as values, for the same reason.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_strip_nulls(k): _strip_nulls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class _ToolCallRecord:
    """Everything `_record_tool_calls` needs to persist one `MessageToolCall`
    row, gathered while `send()` walks the agent loop's events -- kept
    separate from the SSE-facing `ChatToolCallResult` because the DB row
    stores the tool's *full* result (for evaluation/audit, per
    `docs/ARCHITECTURE.md` §5.3), not the excerpt the wire gets.
    """

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult

    @classmethod
    def build(
        cls,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> "_ToolCallRecord":
        """The only way `send()` constructs one -- so every model-derived
        field is made storable at the point it enters this record, not left
        to be discovered at flush time (whole-branch review, Important 1).

        Three shapes were reproduced as an uncaught `DBAPIError` out of
        `send()` *after* the answer had streamed, each rolling the whole turn
        back: a 150-character hallucinated tool name, a 150-character
        provider `tool_call_id` (both columns are `varchar(100)`), and a NUL
        byte anywhere in the arguments. `ToolRegistry.execute` already
        degrades a hallucinated NAME to an error result exactly as
        `docs/ARCHITECTURE.md` §7.3 requires -- and then persistence killed
        the turn anyway, so the "degrade, not crash" guarantee held at two
        layers of three.

        Truncated rather than rejected: this row is an audit record of what
        the model actually did, and the first 100 characters of an absurd
        name identify it perfectly well for anyone reading the table later.
        Rejecting the row would throw away the evidence of the very thing it
        is recording.
        """
        return cls(
            tool_call_id=_strip_nulls(tool_call_id)[:_TOOL_CALL_FIELD_MAX_CHARS],
            tool_name=_strip_nulls(tool_name)[:_TOOL_CALL_FIELD_MAX_CHARS],
            arguments=_strip_nulls(arguments),
            result=result.model_copy(
                update={
                    "content": _strip_nulls(result.content),
                    "data": _strip_nulls(result.data),
                }
            ),
        )


def _serialize_tool_result(result: ToolResult) -> dict[str, Any]:
    """`ToolResult` -> a JSONB-safe dict for `MessageToolCall.result`.

    Not a plain `result.model_dump()`: `CitationPayload` (inside
    `result.citations`) carries `uuid.UUID` fields, which the JSONB column's
    default JSON encoding cannot serialize on its own. Built by hand here
    rather than teaching `CitationPayload` a custom serializer, since this is
    the one place a `CitationPayload` needs to become a plain dict for
    storage rather than round-trip through Pydantic.
    """
    return {
        "content": result.content,
        "data": result.data,
        "citations": [
            {
                "chunk_id": str(c.chunk_id),
                "document_id": str(c.document_id),
                "document_title": c.document_title,
                "rank": c.rank,
                "score": c.score,
                "excerpt": c.excerpt,
                "page": c.page,
            }
            for c in result.citations
        ],
    }


class _LockedSessionTool(AgentTool):
    """Wraps a Phase 4 builtin so every call serializes its use of the
    turn's shared session behind `lock`, instead of touching it with true,
    unguarded concurrency.

    This is the fix for a Critical finding in Task 7's first review round:
    `_build_registry` used to hand `RetrieveKnowledgeTool`/`CreateLeadTool`
    the same `AsyncSession` (`ChatService.session`) at construction, and
    `AgentRunner.run` gathers every call within one step concurrently
    (`asyncio.gather`). `AsyncSession` is not safe for concurrent use: two
    tool bodies both opening `session.begin_nested()` on that one shared
    session raced each other, and the loser corrupted -- sometimes
    surfacing as a fabricated `is_error=True` for a tool that never
    actually failed (`ToolRegistry.execute`'s generic exception handler
    swallowing the SQLAlchemy state error), sometimes as a raw
    `IllegalStateChangeError` escaping the whole turn.

    **Why the fix is a lock, not a session per call.** The first attempt
    gave each call an independent session opened fresh from the engine
    (`tenant_session`, its own transaction). That closes the corruption bug
    but breaks something else that was quietly relying on the shared
    session: `create_lead` on a visitor's very first message needs to see
    THIS turn's own just-created `conversations` row so
    `LeadService.create`'s FK-bypass-guarding `SELECT` finds it -- and that
    row is only `flush()`-ed, not committed, until the whole turn's
    transaction (owned by whoever called `ChatService.send`) finishes. A
    genuinely independent transaction cannot see it: Postgres has no way to
    let one uncommitted transaction read another's uncommitted rows, so
    "give each call its own session" and "a tool started this same turn can
    read what an earlier tool/append_message call in this same turn just
    wrote" are mutually exclusive -- confirmed the hard way, by running
    exactly that scenario (a fresh conversation, `create_lead` called in
    the very first turn) against the per-call-session version and watching
    `LeadService.create` raise `NotFoundError` for a conversation that
    unquestionably exists, just not yet committed.

    A single shared connection cannot physically run two queries at once
    either way, so "true" concurrent database access from two calls in the
    same step was never achievable without one of them waiting -- the only
    question was whether that wait was safe (a lock) or a race
    (`begin_nested()` on a session with no serialization at all, the
    original bug). Serializing access to the ONE genuinely shared,
    non-concurrency-safe resource -- and only that -- preserves every
    guarantee `docs/ARCHITECTURE.md` §7.3 actually names for the loop
    itself, which `AgentRunner` still provides completely unmodified:
    calls are still dispatched together via `asyncio.gather`, one call's
    exception still cannot abort its sibling
    (`return_exceptions=True`), and results are still correlated by id,
    never position. What is serialized is purely how two tool bodies take
    turns on one non-thread-safe database session -- an implementation
    detail of talking to Postgres, not a property of the loop.

    `name`/`description`/`args_model` are read straight off the wrapped
    class -- every Phase 4 tool declares them as class attributes (see e.g.
    `RetrieveKnowledgeTool.name`), not instance state, so no instance is
    needed to know them. The wrapped tool itself is constructed once, per
    turn (not per call): it is stateless beyond the session and (for
    `RetrieveKnowledgeTool`) an optional embedder, and reusing it holds no
    more risk than reusing the session it already shares with every other
    call this turn makes.

    **`timeout_seconds` and the exposed vs. own budget (review round 2,
    item 1).** `ToolRegistry.execute` wraps the ENTIRE `tool.execute(...)`
    call -- lock wait included -- in `asyncio.timeout(tool.timeout_seconds)`.
    If this class exposed the wrapped tool's own declared budget
    unchanged, a call queued behind a slow sibling could be reported as
    "timed out" having never run at all: measured directly, a sibling
    holding the lock ~1.0s alongside a 0.3s budget produced exactly that
    -- `is_error=True`, "timed out after 0.3s", `duration_ms` left
    unset, for a call the registry cancelled mid-*wait*, before this
    class's own `execute` had even acquired the lock.

    So the two budgets are deliberately different now. `self.timeout_seconds`
    (what `ToolRegistry.execute` enforces) is widened by
    `_LOCK_WAIT_BUDGET_SECONDS` -- a generous, explicitly soft allowance for
    realistic queueing behind however many sibling calls one step happens
    to gather, not a precise worst case (there is no static bound on how
    many calls one step can contain). `self._own_timeout_seconds` -- the
    wrapped tool's real, unwidened budget -- is enforced separately, by a
    SECOND, nested `asyncio.timeout()` opened fresh only after the lock is
    actually acquired, so it measures the tool's own work, never its
    queueing. The two failures are worded differently for the model
    reading them, deliberately: "timed out after Xs" (the outer,
    registry-level bound -- genuinely stuck, including any wait) vs. "took
    longer than Xs to run, not counting time spent waiting for another
    tool call in this turn" (this class's own bound -- the tool itself ran
    long once it got the chance). Keeping the outer bound at all, rather
    than removing it in favour of the inner one alone, is deliberate too:
    it is the only thing that still catches a call stuck for a
    structural reason (a leaked lock, a hung sibling) rather than its own
    slow work, which the inner bound by construction cannot.

    `duration_ms` still measures the call's FULL wall-clock time, wait
    included -- unchanged from round 1's reasoning, and the two rulings
    are not in tension: "how long did this call take" (duration_ms, for
    the UI/audit trail) and "how long was this call allowed to actually
    run before being cut off" (`_own_timeout_seconds`, for whether it is
    treated as a failure) are different questions.
    """

    def __init__(
        self, tool_cls: type[AgentTool], session: AsyncSession, lock: asyncio.Lock
    ) -> None:
        self.name = tool_cls.name
        self.description = tool_cls.description
        self.args_model = tool_cls.args_model
        self._own_timeout_seconds = tool_cls.timeout_seconds
        self.timeout_seconds = tool_cls.timeout_seconds + _LOCK_WAIT_BUDGET_SECONDS
        # Every Phase 4 builtin's constructor takes the session as its sole
        # required argument (see `RetrieveKnowledgeTool`/`CreateLeadTool`)
        # -- not expressible on the `AgentTool` ABC itself, which declares
        # no `__init__` at all, a tool's construction requirements being
        # its own business and not the interface's.
        self._inner = tool_cls(session)  # type: ignore[call-arg]
        self._session = session
        self._lock = lock

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        # Holds the lock for the tool's ENTIRE execution, not just its
        # database statements -- simpler and still correct, at the cost of
        # serializing any non-DB work a future tool body might do (a
        # rate-limit check, say) alongside its DB work too.
        started = time.monotonic()
        async with self._lock:
            wait_ms = int((time.monotonic() - started) * 1000)
            if wait_ms:
                # Logged, not merely absorbed into `duration_ms`: an
                # operator watching this tool suddenly get slow needs to be
                # able to tell "its own work got slow" apart from "it is
                # queued behind a sibling", and only this line says which.
                logger.info(
                    "tool_call_waited_for_shared_session",
                    tool_name=self.name,
                    wait_ms=wait_ms,
                    **_tool_log_context(ctx),
                )
            result = await self._run_bounded(args, ctx)
        duration_ms = int((time.monotonic() - started) * 1000)
        return result.model_copy(update={"duration_ms": duration_ms})

    def _timeout_result(self) -> ToolResult:
        return ToolResult(
            content=(
                f"'{self.name}' took longer than {self._own_timeout_seconds}s to "
                "run (not counting time spent waiting for another tool call in "
                "this turn) and was stopped."
            ),
            is_error=True,
        )

    async def _run_bounded(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Run the wrapped tool under its own budget, in a way that cannot
        leave the caller's session unusable for the rest of the turn --
        whatever the outcome (whole-branch review, Critical 2).

        **The bug this shape exists to prevent.** The previous version put
        `asyncio.timeout(self._own_timeout_seconds)` straight around
        `self._inner.execute`. When that fired while an asyncpg statement
        was in flight on the shared session -- a 10s hybrid retrieval on a
        large corpus, which `docs/PHASE-4.md` §7 treats as an ordinary bad
        day, not an exotic input -- the task was cancelled mid-statement and
        SQLAlchemy invalidated the connection. A savepoint is no help: it
        recovers a transaction from a *statement error*, not from a
        cancelled, still-in-flight statement on an invalidated connection.
        `ChatService.send` then died on its very next statement with
        `PendingRollbackError` -- not an `AppError`, so it escaped as an
        uncaught exception and the whole turn rolled back, losing the user's
        message, the answer the browser had already rendered, the tool rows,
        the citations and the usage row. `app/api/chat.py::_pump`'s own
        docstring describes this exact hazard and solves it for the
        heartbeat; the tool timeout reintroduced it a layer down.

        **The shape.** Three things, in this order:

        1. `SET LOCAL statement_timeout` bounds the tool's *database* work
           at the database, at the tool's own budget. An overrun therefore
           arrives as an ordinary `QueryCanceledError` (SQLSTATE 57014) --
           a statement error, which savepoints recover from perfectly -- and
           never as an event-loop cancellation. `SET LOCAL` is
           transaction-scoped, so it is undone automatically when the
           savepoint below rolls back, and cleared explicitly in the
           `finally` when it does not (a released savepoint does NOT undo a
           `SET LOCAL`, and the caller's own later writes must not inherit a
           tool's budget).
        2. A savepoint around the whole call, so even a tool that opens none
           of its own (`RetrieveKnowledgeTool` and `CreateLeadTool` both do;
           a future one might not) cannot abort the turn's transaction.
        3. `asyncio.timeout` is kept, but only as the net for work that is
           NOT a database statement -- a future tool's HTTP call, a
           pure-Python loop -- and widened by `_NON_DB_GRACE_SECONDS` so
           that step 1 always fires first for DB work. By the time this one
           fires, the tool is provably not blocked in a statement, which is
           exactly the condition under which cancelling it is safe.

        The residual case is a tool that runs *many* statements, each inside
        its own `statement_timeout` but summing past the grace: the net then
        fires, possibly mid-statement, and the session can still be lost.
        That is why `ChatService.send` additionally treats a failed
        persistence as a first-class in-band failure (see its own
        `except SQLAlchemyError` there) rather than letting it escape --
        containment behind prevention, because no tool failure of any kind
        may cost a turn that already reached the user.
        """
        statement_timeout_ms = max(1, int(self._own_timeout_seconds * 1000))
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    text(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
                )
                async with asyncio.timeout(self._own_timeout_seconds + _NON_DB_GRACE_SECONDS):
                    return await self._inner.execute(args, ctx)
        except TimeoutError:
            logger.warning(
                "tool_call_execution_timed_out",
                tool_name=self.name,
                timeout_seconds=self._own_timeout_seconds,
                bound="event_loop",
                **_tool_log_context(ctx),
            )
            return self._timeout_result()
        except DBAPIError as exc:
            if not _is_statement_timeout(exc):
                # Any other DBAPI failure is the tool's own to report --
                # `ToolRegistry.execute` already turns it into
                # `ToolResult(is_error=True)` with the traceback logged, and
                # the savepoint above has already rolled back, so the
                # caller's session is usable either way.
                raise
            logger.warning(
                "tool_call_execution_timed_out",
                tool_name=self.name,
                timeout_seconds=self._own_timeout_seconds,
                bound="statement_timeout",
                **_tool_log_context(ctx),
            )
            return self._timeout_result()
        finally:
            await self._clear_statement_timeout()

    async def _clear_statement_timeout(self) -> None:
        """Undo this call's `SET LOCAL statement_timeout` for the rest of the
        turn's transaction.

        Needed only on the paths where the savepoint was *released* rather
        than rolled back (a successful call, mostly): Postgres undoes a
        `SET LOCAL` when the savepoint it was issued inside rolls back, but
        not when it is released, and the caller's own later writes must not
        silently inherit a tool's budget. Issuing it on the rollback paths
        too is harmless and keeps this one line rather than a state flag.

        Best-effort by design: if the session is already unusable there is
        nothing left for this to fix, and raising here would replace a
        reportable tool failure with an unreportable one.
        """
        try:
            await self._session.execute(text("SET LOCAL statement_timeout = DEFAULT"))
        except SQLAlchemyError:  # pragma: no cover - only on an already-lost session
            logger.warning("tool_statement_timeout_reset_failed", tool_name=self.name)


class ChatService:
    """Turns an agent, its configured prompt, and a user message into a
    streamed reply that is persisted with usage and cost.

    See `docs/PHASE-2.md` §3, §5, §6 and `docs/PHASE-4.md` §2, §4 for the
    reasoning behind the ordering below; the short version is that every
    write goes through `ConversationService`/`AgentService`/`PromptService`
    rather than the ORM directly, because those are what close the
    FK-bypasses-RLS hole documented on `ConversationService.record_usage`.

    Task 7 replaces the single provider completion with `AgentRunner`'s
    multi-step loop, and replaces the unconditional retrieval prefix with
    `retrieve_knowledge`, a tool the model chooses to call -- see
    `docs/PHASE-4.md` §2 for why that change is the point of the phase.
    """

    def __init__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        provider_override: LLMProvider | None = None,
        history_window: int = DEFAULT_HISTORY_WINDOW,
    ) -> None:
        self.session = session
        self.tenant = tenant
        self._provider_override = provider_override
        self._history_window = history_window
        self._agents = AgentService(session, tenant)
        self._prompts = PromptService(session, tenant)
        self._conversations = ConversationService(session, tenant)

    async def send(
        self,
        agent_id: uuid.UUID,
        user_text: str,
        conversation_id: uuid.UUID | None = None,
        channel: ConversationChannel = ConversationChannel.API,
        override_provider: str | None = None,
        override_model: str | None = None,
    ) -> AsyncIterator[ChatEvent]:
        """`override_provider`/`override_model` answer this one turn with
        something other than the agent's configured pair, without writing
        anything back to the agent -- what the playground's model picker sends
        so a model can be tried before it is committed to. Distinct from
        `provider_override` on `__init__`, which injects a whole `LLMProvider`
        object (tests, and nothing else); these are the *names* a caller may
        pass per request.
        """
        # Step 1: load the agent and its config. Both raise NotFoundError
        # (cross-tenant, or a config row that does not exist) before any
        # conversation row is created -- a misconfigured or foreign agent_id
        # must never leave a partial conversation behind. `config` is now
        # actually used below (`max_agent_steps`), unlike the pre-Task-7
        # version, which only called this for its 404 side effect.
        agent = await self._agents.get_agent(agent_id)
        config = await self._agents.get_config(agent_id)

        # What actually answers this turn. Resolved once, here, and used
        # everywhere below in place of `agent.provider`/`agent.model` -- the
        # persisted message and the `usage_events` row included, so billing
        # data records what was billed rather than what the agent is
        # configured for.
        provider_name = override_provider or agent.provider
        model_name = override_model or agent.model

        # Resolve the provider before anything is written: a missing API key
        # (LLMConfigurationError) must surface as an operator problem, not
        # disguise itself as a conversation that was created and then failed.
        # An override naming a provider with no key configured fails here, in
        # the same pre-stream span, for the same reason -- so it comes back as
        # a JSON error envelope and not an in-band event.
        provider = self._provider_override or get_provider(provider_name)

        system_prompt, prompt_version_id = await self._resolve_system_prompt(agent)

        # Step 4: create or load the conversation (404s cross-tenant for an
        # existing id, via ConversationService.get).
        created = conversation_id is None
        if created:
            conversation = await self._conversations.create(
                agent_id, CreateConversationInput(channel=channel)
            )
        else:
            assert conversation_id is not None
            conversation = await self._conversations.get(conversation_id)
            if conversation.agent_id != agent_id:
                # Review round 1, Important finding 2: this used to be
                # silently tolerated -- `agent`/`provider_name`/`model_name`/
                # `system_prompt` above are all already resolved from the
                # REQUEST's `agent_id`, not `conversation.agent_id`, so a
                # caller who names a `conversation_id` belonging to a
                # different agent in the same org got that other agent's
                # prompt, provider, model, temperature and max_tokens
                # applied to it -- and, worse, any tool `agent_tools` grants
                # to the REQUEST's agent but not the conversation's own
                # (`create_lead`, say) ran anyway, because `ToolContext.
                # agent_id` is `conversation.agent_id` (correctly
                # server-derived, per the same review's confirmed-correct
                # finding), so a lead the request's agent was never granted
                # `create_lead` for could still be attributed to the
                # conversation's agent, who never actually made the call.
                # Rejecting outright, rather than silently preferring
                # `conversation.agent_id` for everything, is deliberate: a
                # mismatch here is a caller bug (or an attempt to borrow
                # another agent's tool grants), not a case with a sensible
                # default to fall back to. `NotFoundError`, not a more
                # specific error, for the same reason `ConversationService.
                # get` never distinguishes "not yours" from "does not
                # exist" -- confirming which agents share an org is not
                # this error's job to leak.
                raise NotFoundError("conversation not found")

        # The assistant's message id is minted now, before its content is
        # known, so `ChatMessageStart` can tell the caller which message is
        # about to stream -- and the row persisted at the end (success or
        # failure) is created under this same id.
        message_id = uuid7()
        yield ChatMessageStart(
            conversation_id=conversation.id, message_id=message_id, created=created
        )

        # History is fetched *before* the new user message is persisted, so
        # it holds only prior turns; the new user text is appended to the
        # request separately below. Fetching after persisting would instead
        # count the brand-new user turn against the window.
        history_rows = await self._conversations.history(
            conversation.id, limit=self._history_window
        )
        await self._conversations.append_message(
            conversation.id, AppendMessageInput(role=MessageRole.USER, content=user_text)
        )

        # Only user/assistant turns are valid wire-format participants: the
        # system prompt is already injected separately above, and a
        # tool-role row's own content_blocks are not replayed into a later
        # turn's history yet -- see the module docstring in
        # `app/agents/runner.py` and `docs/ARCHITECTURE.md` §5.3 ("tool
        # results are truncated in history"); reconstructing that bounded
        # replay is future work this task does not attempt, so a row is
        # skipped rather than guessed at, exactly as before Task 7.
        request_messages: list[LLMMessage] = []
        for row in history_rows:
            if row.role is MessageRole.USER:
                request_messages.append(LLMMessage.text("user", row.content or ""))
            elif row.role is MessageRole.ASSISTANT:
                request_messages.append(LLMMessage.text("assistant", row.content or ""))
        request_messages.append(LLMMessage.text("user", user_text))

        # `ctx.agent_id` is read off `conversation.agent_id` -- the
        # conversation's own row, loaded above under this tenant's RLS --
        # never off the `agent_id` parameter this method was called with.
        # The two agree in every call this codebase makes today, but
        # `LeadService.create` (Task 6) deliberately does not re-check
        # `agent_id` against the conversation it is handed, relying on the
        # invariant that `ToolContext.agent_id` always comes from the
        # conversation's own row. This is the one place a `ToolContext` is
        # constructed in production, so this is where that invariant is
        # actually made true, not merely assumed.
        tool_ctx = ToolContext(
            organization_id=self.tenant.organization_id,
            agent_id=conversation.agent_id,
            conversation_id=conversation.id,
            request_id=self.tenant.request_id,
            visitor_id=conversation.visitor_id,
        )
        tool_names = await self._resolve_enabled_tool_names(agent_id)
        registry = self._build_registry(tool_names)
        runner = AgentRunner(
            provider,
            registry,
            model=model_name,
            max_tokens=agent.max_tokens,
            temperature=agent.temperature,
            max_steps=config.max_agent_steps,
        )

        accumulated: list[str] = []
        usage = Usage()
        tool_calls_by_id: dict[str, ToolUseBlock] = {}
        tool_call_records: list[_ToolCallRecord] = []
        citations: list[CitationPayload] = []
        step_limit_hit = False
        chat_error: ChatError | None = None
        started_at = time.monotonic()

        try:
            # `AgentStepLimit`/`AgentUsage` are handled inline below rather
            # than via the `elif` chain some of their siblings use, purely
            # because they need no further branching -- see each comment.
            # `StreamEvent`'s `ErrorEvent` remains unhandled for the same
            # reason it was before Task 7: no provider today signals failure
            # by yielding an in-band error event; every provider raises
            # (caught below as `AppError`) instead.
            async for event in runner.run(system_prompt, request_messages, tool_names, tool_ctx):
                if isinstance(event, AgentTextDelta):
                    accumulated.append(event.text)
                    yield ChatTextDelta(text=event.text)
                elif isinstance(event, AgentToolCallStart):
                    for call in event.calls:
                        tool_calls_by_id[call.id] = call
                    yield ChatToolCallStart(
                        calls=[
                            ChatToolCall(id=call.id, name=call.name, arguments=call.input)
                            for call in event.calls
                        ]
                    )
                elif isinstance(event, AgentToolCallEnd):
                    step_citations: list[CitationPayload] = []
                    chat_results: list[ChatToolCallResult] = []
                    for result in event.results:
                        # `tool_calls_by_id` is populated by every
                        # `AgentToolCallStart` this loop has already seen,
                        # and `AgentRunner.run`'s own contract guarantees a
                        # `AgentToolCallEnd` never arrives for a call id its
                        # matching `AgentToolCallStart` did not just carry --
                        # so this lookup cannot miss in practice. `.get(...)`
                        # rather than `[...]` only to keep that assumption
                        # from becoming a `KeyError` if it is ever wrong.
                        matching_call = tool_calls_by_id.get(result.tool_call_id)
                        tool_name = matching_call.name if matching_call is not None else "unknown"
                        arguments = matching_call.input if matching_call is not None else {}
                        tool_call_records.append(
                            _ToolCallRecord.build(
                                tool_call_id=result.tool_call_id,
                                tool_name=tool_name,
                                arguments=arguments,
                                result=result.result,
                            )
                        )
                        chat_results.append(
                            ChatToolCallResult(
                                tool_call_id=result.tool_call_id,
                                tool_name=tool_name,
                                result=excerpt_text(result.result.content),
                                is_error=result.result.is_error,
                            )
                        )
                        # An error result's `citations` is always empty (no
                        # tool populates both), but the `is_error` guard is
                        # explicit anyway: citations must never be reported
                        # for a call that did not actually succeed.
                        if not result.result.is_error and result.result.citations:
                            step_citations.extend(result.result.citations)
                    yield ChatToolCallEnd(results=chat_results)
                    if step_citations:
                        citations.extend(step_citations)
                        yield ChatCitations(citations=step_citations)
                elif isinstance(event, AgentUsage):
                    # The running total, per step -- see `AgentUsage`'s own
                    # docstring in `app/agents/runner.py`. Reassigning (not
                    # summing) is correct here for the same reason: the value
                    # already *is* the cumulative total as of this step.
                    usage = event.usage
                elif isinstance(event, AgentStepLimit):
                    # Surfaced, not swallowed (docs/PHASE-4.md §7's risk
                    # table): logged here, and recorded on the persisted
                    # message via `finish_reason` below, rather than a new
                    # SSE event of its own -- the turn still completes with
                    # whatever text/tool results were produced before the
                    # cap, and `ChatMessageEnd` already carries everything a
                    # client needs to render that. A dedicated in-band event
                    # is not exercised by any Task 7 test and would be pure
                    # surface area without a documented consumer yet.
                    step_limit_hit = True
                    logger.warning(
                        "agent_step_limit_reached",
                        agent_id=str(agent_id),
                        conversation_id=str(conversation.id),
                        max_steps=config.max_agent_steps,
                    )
        except AppError as exc:
            chat_error = ChatError(code=exc.code, message=exc.message)

        latency_ms = int((time.monotonic() - started_at) * 1000)
        final_text = "".join(accumulated)
        # AgentRunner is always constructed with `model=model_name` above,
        # and every provider (FakeProvider included) echoes `request.model`
        # back unchanged in every event -- so this is not a guess, it is the
        # value that was actually requested on every step of this turn. (A
        # provider that ever normalizes/rewrites the model string in its own
        # response would need `AgentUsage` or a sibling event to carry it
        # forward; none does today, and `AgentRunner` does not expose a
        # per-step model field for `send()` to read instead.)
        model_used = model_name

        # Everything below writes. Wrapped, because a turn that already
        # reached the user must not be lost to an exception raised while
        # saving it (whole-branch review, Criticals 2 and Important 1):
        # model-derived tool-call fields are sanitised at
        # `_ToolCallRecord` construction, and a tool timeout can no longer
        # cancel an in-flight statement on this session -- but neither
        # guarantee is worth betting the turn on, and before this the
        # entire block after `except AppError` closed was unprotected: any
        # `SQLAlchemyError` here escaped `send()` uncaught, `_pump` turned
        # it into a generic internal-error SSE event, and the transaction
        # rolled back -- the user's message, the streamed answer, the tool
        # rows, the citations and the usage row all gone AFTER the answer
        # was on screen. `final_event` is computed inside and yielded
        # after, so the failure path replaces the terminal event rather
        # than arriving alongside one already sent.
        final_event: ChatEvent
        try:
            if chat_error is None:
                cost = estimate_cost(model_used, usage)
                await self._conversations.append_message(
                    conversation.id,
                    AppendMessageInput(
                        id=message_id,
                        role=MessageRole.ASSISTANT,
                        content=final_text,
                        prompt_version_id=prompt_version_id,
                        provider=provider_name,
                        model=model_used,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                        latency_ms=latency_ms,
                        finish_reason="step_limit_reached" if step_limit_hit else None,
                    ),
                )
                # Same transaction as the assistant message above.
                await self._record_tool_calls(message_id, tool_call_records)
                await self._record_citations(message_id, citations)
                await self._conversations.record_usage(
                    RecordUsageInput(
                        agent_id=agent.id,
                        conversation_id=conversation.id,
                        kind=UsageKind.LLM,
                        provider=provider_name,
                        model=model_used,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                    )
                )
                if step_limit_hit:
                    # §5.1's pseudocode: `yield Error("step_limit_reached")`.
                    # Review round 1, Important finding 4: an earlier version of
                    # this method treated a step-limit hit as an ordinary
                    # success -- everything above (message, tool calls,
                    # citations, usage) is still persisted, because it is all
                    # real, but the client received a normal `message_end` for
                    # what could be a completely empty assistant message, with
                    # nothing on the wire distinguishing "the model finished"
                    # from "the loop gave up mid-thought". Yielding `ChatError`
                    # instead of `ChatMessageEnd` here is the explicit,
                    # in-band signal §4/§5.1 both call for; `finish_reason` on
                    # the persisted message (set above) is the same fact for
                    # anything reading history afterward.
                    final_event = ChatError(
                        code="step_limit_reached",
                        message=(
                            f"The assistant reached its step limit "
                            f"({config.max_agent_steps}) while still requesting "
                            "tools and could not finish answering."
                        ),
                    )
                else:
                    final_event = ChatMessageEnd(
                        usage=usage,
                        cost_usd=cost,
                        latency_ms=latency_ms,
                        model=model_used,
                        prompt_version_id=prompt_version_id,
                    )
            else:
                # Do not discard the partial reply: some of it already reached
                # the browser, so a history that disagrees with the screen is
                # worse than an incomplete one. No usage_events row is written
                # here -- there is no reliable usage figure for a stream that
                # never reached its message_end/usage event.
                await self._conversations.append_message(
                    conversation.id,
                    AppendMessageInput(
                        id=message_id,
                        role=MessageRole.ASSISTANT,
                        content=final_text,
                        prompt_version_id=prompt_version_id,
                        provider=provider_name,
                        model=model_used,
                        latency_ms=latency_ms,
                        error=chat_error.message,
                    ),
                )
                # Recorded here too, not just on the success path above: any
                # tool call that already completed -- and the assistant message
                # this turn produced, partial and marked `error=` but persisted
                # -- happened before the failure, and per
                # docs/PHASE-3.md §5 this table's whole reason for existing is
                # to make "did it answer from the sources?" answerable after the
                # fact. The identical reasoning now applies to
                # `message_tool_calls`: a tool call that already ran and
                # returned before a later step's provider failure is real
                # information about this turn, not something the failure should
                # erase.
                await self._record_tool_calls(message_id, tool_call_records)
                await self._record_citations(message_id, citations)
                final_event = chat_error
        except SQLAlchemyError:
            # The one exception class that means "the database said no", as
            # opposed to a provider or an application error: a constraint or
            # column-width violation on a model-derived field, a JSONB value
            # Postgres rejects, or a session already lost before this block
            # began. Logged with the traceback, never interpolated into the
            # wire message -- the same rule `app/api/chat.py`'s
            # `_INTERNAL_ERROR_MESSAGE` states for the layer above.
            #
            # Nothing is re-raised: the caller's transaction is left for
            # whoever owns it to roll back, and the client gets a terminal
            # in-band event instead of a stream that just stops. This is the
            # containment half of Critical 2 -- prevention (per-tool
            # `statement_timeout`, see `_LockedSessionTool._run_bounded`) is
            # what stops the session being lost in the first place.
            logger.exception(
                "chat_turn_persistence_failed",
                agent_id=str(agent_id),
                conversation_id=str(conversation.id),
                message_id=str(message_id),
            )
            final_event = ChatError(code="internal_error", message=_PERSISTENCE_FAILED_MESSAGE)

        yield final_event

    async def _resolve_enabled_tool_names(self, agent_id: uuid.UUID) -> list[str]:
        """Which tool names this agent may call, per `docs/ARCHITECTURE.md`
        §2.3's two-layer predicate: RLS on `self.session` is Layer 2, and the
        explicit `organization_id` filters below (on *both* `agent_tools` and
        `tools`) are Layer 1 -- belt-and-braces against a future caller that
        hands this a session RLS does not actually apply to, exactly like
        every other query in this codebase that already does this (see
        `app/rag/retrieve.py`'s module docstring).

        Restricted to `ToolType.BUILTIN`: Phase 4 ships no HTTP/MCP tool
        adapter (`docs/ARCHITECTURE.md` §8 -- MCP is explicitly Phase 6), so
        an `http`/`mcp` row surviving this filter would only ever resolve to
        a name `AgentRunner._resolve_specs` cannot find in the Python
        `ToolRegistry` and silently skips, logging a warning for a
        misconfiguration that was not actually one. Filtering here means
        that path is never reached for a tool type nobody can call yet.

        **Shadowing.** Task 3's schema deliberately allows an org-scoped
        `tools` row and a global (`organization_id IS NULL`) builtin to
        share a `name`, and leaves resolution order to this task. The rule
        here: an org-scoped row **fully shadows** a global builtin of the
        same name for this agent -- not just its `config`/`overrides`, but
        whether the agent may call it at all. If the agent's `agent_tools`
        link to the org-scoped row is disabled, the name is excluded even
        though a separate link to the global builtin might be enabled.
        Reasoning: an organization that owns a `tools` row named
        `retrieve_knowledge` has taken deliberate ownership of that name --
        modelling, say, a customized retrieval tool that should fully
        replace the stock one for their agents -- and a still-enabled global
        link of the same name is exactly the platform default that
        ownership exists to override. Implemented by sorting each name's
        rows with the org-scoped one first (`ORDER BY ... organization_id IS
        NULL`, which places `false` -- i.e. NOT NULL, org-scoped -- before
        `true` in Postgres) and keeping only the first row seen per name.

        A `tools.is_enabled = false` row is dropped in SQL, not carried
        through this shadowing logic: a fully disabled tool *definition* can
        never be called regardless of any agent's link to it, so it has
        nothing left to shadow with. `agent_tools.is_enabled`, by contrast,
        is read in Python rather than filtered in SQL, specifically so a
        *disabled* org-scoped link can still shadow (and suppress) an
        enabled global one -- filtering it out in SQL would make that row
        invisible to the shadowing decision entirely.
        """
        stmt = (
            select(Tool.name, Tool.organization_id, AgentToolLink.is_enabled)
            .select_from(AgentToolLink)
            .join(Tool, Tool.id == AgentToolLink.tool_id)
            .where(
                AgentToolLink.agent_id == agent_id,
                AgentToolLink.organization_id == self.tenant.organization_id,
                Tool.is_enabled.is_(True),
                Tool.type == ToolType.BUILTIN,
                or_(
                    Tool.organization_id == self.tenant.organization_id,
                    Tool.organization_id.is_(None),
                ),
            )
            .order_by(Tool.name, Tool.organization_id.is_(None))
        )
        rows = (await self.session.execute(stmt)).all()

        resolved: dict[str, bool] = {}
        shadowed: set[str] = set()
        for name, tool_organization_id, link_is_enabled in rows:
            if name in shadowed:
                continue
            resolved[name] = bool(link_is_enabled)
            if tool_organization_id is not None:
                shadowed.add(name)
        return [name for name, enabled in resolved.items() if enabled]

    def _build_registry(self, tool_names: list[str]) -> ToolRegistry:
        """The builtins this agent is actually GRANTED -- and only those --
        registered fresh per turn, each wrapped in `_LockedSessionTool`,
        sharing one `asyncio.Lock` created fresh here, so no two calls this
        turn ever touch `self.session` concurrently. See
        `_LockedSessionTool`'s docstring for why a lock, not a session per
        call: `AsyncSession` is not safe for concurrent use,
        `AgentRunner.run` gathers a step's calls with `asyncio.gather`, and
        a genuinely independent per-call session cannot see this same
        turn's own not-yet-committed writes (the conversation `create_lead`
        needs to find on a visitor's very first message).

        **`tool_names` is the enforcement point, not a display list**
        (whole-branch review, Critical 1). An earlier version registered
        every builtin unconditionally and passed the resolved names to
        `AgentRunner` only to decide which `ToolSpec`s the model is *shown*.
        That made the `agent_tools` grant advisory: `ToolRegistry.execute`
        looks a call's name up in the registry, so a model that named
        `create_lead` without being offered it -- a hallucination, or an
        instruction smuggled into a document that `retrieve_knowledge` fed
        back as tool-result content (`app/tools/retrieve.py` names that
        injection hazard in its own comment) -- ran the tool and wrote a
        real `Lead` row. Task 7b's "off by default", Task 8's per-agent
        toggle and `tools.is_enabled` as a platform kill switch were all
        unenforced, and `docs/ARCHITECTURE.md` §7.2's "an agent may call a
        builtin only if an enabled `agent_tools` row links it" was false.

        Registering only what was resolved makes the registry itself the
        authorization boundary: an ungranted name is simply not in the dict,
        so `ToolRegistry.execute` returns its existing unknown-name
        `ToolResult(is_error=True)` and nothing runs. That path already
        degrades correctly (§7.3: a model naming a tool that does not exist
        is its mistake to be told about, not a turn to end), so an
        ungranted call is routed *there* rather than to a new refusal
        branch of its own -- one fewer shape for the model to have to
        understand, and the wording deliberately does not confirm that a
        tool by that name exists elsewhere in the platform.

        One lock per turn, not one global lock: two DIFFERENT turns (two
        different `ChatService.send()` calls, each with its own session)
        must never contend on each other's lock -- only calls sharing the
        SAME session need to.

        Cheap: `ToolRegistry.register` does no I/O, only the
        tenant-leak-check on each `args_model` (already paid once per class
        at import time in practice, since Python caches the class object --
        this just re-runs it).
        """
        granted = set(tool_names)
        lock = asyncio.Lock()
        registry = ToolRegistry()
        for tool_cls in _BUILTIN_TOOL_CLASSES:
            if tool_cls.name in granted:
                registry.register(_LockedSessionTool(tool_cls, self.session, lock))
        return registry

    async def _assert_message_belongs_to_tenant(self, message_id: uuid.UUID) -> None:
        """Shared by `_record_citations` and `_record_tool_calls`: a Postgres
        FK constraint check runs with elevated privileges and does not
        consult this session's RLS policy, so an INSERT into either
        `message_citations` or `message_tool_calls` would happily attach a
        row to another tenant's `message_id` even though a plain SELECT
        under this session's RLS sees zero rows for it. In `ChatService.send`'s
        own call path `message_id` is always one this same call just minted
        for this same tenant, so this specific check can never actually fire
        today -- but the FK-bypass hazard is a property of both tables, not
        of today's one caller, and this is what keeps that true regardless
        of who calls either method next.
        """
        result = await self.session.execute(
            select(ConversationMessage.id).where(
                ConversationMessage.id == message_id,
                ConversationMessage.organization_id == self.tenant.organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("message not found")

    async def _record_tool_calls(
        self, message_id: uuid.UUID, records: list[_ToolCallRecord]
    ) -> None:
        """Writes one `MessageToolCall` row per call the model made this
        turn, in the caller's open transaction, after the scoped ownership
        check on `message_id` (see `_assert_message_belongs_to_tenant`).

        `result`/`error_message` store the tool's *full* outcome -- unlike
        `ChatToolCallResult` on the SSE wire, this is not excerpted: per
        `docs/ARCHITECTURE.md` §5.3, this table is what evaluation and the
        UI's own transcript view read the complete payload from, while the
        model (via history) and the live stream (via `ChatToolCallResult`)
        both see a bounded version.

        `duration_ms` comes straight off `record.result` -- populated by
        `_LockedSessionTool.execute`, which times the whole call (including
        any wait for a sibling call holding the shared session's lock), not
        measured here.
        """
        if not records:
            return
        await self._assert_message_belongs_to_tenant(message_id)

        self.session.add_all(
            [
                MessageToolCall(
                    id=uuid7(),
                    organization_id=self.tenant.organization_id,
                    message_id=message_id,
                    tool_call_id=record.tool_call_id,
                    tool_name=record.tool_name,
                    arguments=record.arguments,
                    result=_serialize_tool_result(record.result),
                    is_error=record.result.is_error,
                    error_message=record.result.content if record.result.is_error else None,
                    duration_ms=record.result.duration_ms,
                )
                for record in records
            ]
        )
        await self.session.flush()

    async def _record_citations(self, message_id: uuid.UUID, chunks: list[CitationPayload]) -> None:
        """Writes one `MessageCitation` row per citation a tool returned this
        turn, in the caller's open transaction, after the scoped ownership
        check on `message_id` (see `_assert_message_belongs_to_tenant`).

        `chunk_id`/`document_id` need no equivalent tenant check of their
        own: both come straight out of `RetrievalService.retrieve` (via
        `RetrieveKnowledgeTool`/`build_citation`), which is itself two-layer
        tenant-scoped (see `app/rag/retrieve.py`), so a value reaching here
        has already been proven to belong to this organization.

        `document_title` and `excerpt` are written alongside those ids
        rather than left to a join, because the ids are `ON DELETE SET
        NULL`: a re-ingest or a document delete nulls them out and the
        citation has to stay legible on its own. See `MessageCitation`'s
        own docstring.

        `rank` is renumbered sequentially across `chunks` here
        (`enumerate(chunks, start=1)`), not read off `chunk.rank` --
        review round 1, Important finding 3. `chunk.rank` is *per-call*
        (`RetrievalService.retrieve` numbers each call's own results
        `1..top_k`), and `chunks` here is the whole turn's citations
        accumulated across every `retrieve_knowledge` call the model made
        (`ChatService.send` extends `citations` once per qualifying
        `AgentToolCallEnd`) -- persisting `chunk.rank` unchanged meant a
        turn with two calls, each returning two results, wrote ranks
        `[1, 1, 2, 2]`, not `[1, 2, 3, 4]`. There is no unique constraint on
        `(message_id, rank)` to have caught this; renumbering at the one
        place these are actually persisted is what makes "rank" mean
        "this citation's position among everything this message cited",
        which is what a UI listing a message's sources needs it to mean.
        The SSE-facing `ChatCitations` events themselves are unaffected --
        each is emitted per call and keeps that call's own local rank,
        which is correct there: one event describes one call's own ranked
        results, not the whole turn's.
        """
        if not chunks:
            return
        await self._assert_message_belongs_to_tenant(message_id)

        self.session.add_all(
            [
                MessageCitation(
                    id=uuid7(),
                    organization_id=self.tenant.organization_id,
                    message_id=message_id,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    excerpt=chunk.excerpt,
                    rank=rank,
                    score=chunk.score,
                )
                for rank, chunk in enumerate(chunks, start=1)
            ]
        )
        await self.session.flush()

    async def _resolve_system_prompt(self, agent: Agent) -> tuple[str, uuid.UUID | None]:
        """Step 2+3: resolve the active prompt version (or the default) and
        render it. `prompt_version_id` is `None` exactly when the fallback
        default was used -- that is what lets every assistant message be
        traced back to the exact prompt text that produced it, per
        PHASE-2.md §6, without inventing a version id for text that has none.
        """
        declared_variables: dict[str, str]
        if agent.prompt_id is not None:
            version = await self._prompts.active_version(agent.prompt_id)
            template = version.system_prompt
            declared_variables = {k: str(v) for k, v in version.variables.items()}
            prompt_version_id: uuid.UUID | None = version.id
        else:
            template = DEFAULT_SALES_SYSTEM_PROMPT
            declared_variables = {}
            prompt_version_id = None

        organization = await self._organization()
        variables = {
            **declared_variables,
            "company_name": organization.name,
            "agent_name": agent.name,
        }
        return _render_template(template, variables), prompt_version_id

    async def _organization(self) -> Organization:
        result = await self.session.execute(
            select(Organization).where(Organization.id == self.tenant.organization_id)
        )
        return result.scalar_one()
