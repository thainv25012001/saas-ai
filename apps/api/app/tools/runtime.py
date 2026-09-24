"""The one tool runtime `ChatService` and the MCP server both call.

`docs/PHASE-7.md` §5, "One tool runtime, two callers": the code that resolves
an agent's granted tools, builds the registry of only those tools, and runs
each call under a savepoint with a `statement_timeout` used to live in
`app/chat/service.py` as `ChatService._resolve_enabled_tool_names`,
`ChatService._build_registry` and `_LockedSessionTool`. It moves here,
unchanged in behaviour (Phase 7 Task 1), so a later MCP server calls exactly
the code a chat turn calls rather than a copy that can drift -- copying it
would give MCP the timeouts and grant checks as they stood on the day of the
copy, not as they evolve afterwards. `ChatService` keeps thin wrapper methods
of the old names where existing tests still call them, and both wrappers and
the MCP server call the functions below directly.
"""

import asyncio
import time
import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.db.builtin_tools import first_row_per_name
from app.db.models import AgentToolLink, Tool, ToolType
from app.rag.retrieve import CitationPayload
from app.tools.base import AgentTool, ToolContext, ToolResult
from app.tools.leads import CreateLeadTool
from app.tools.products import GetProductTool, SearchProductsTool
from app.tools.registry import ToolRegistry
from app.tools.retrieve import RetrieveKnowledgeTool

logger = get_logger(__name__)

# Every builtin `build_granted_registry` knows how to construct, keyed implicitly by
# each class's own `name`. A tuple, not a dict, so the class stays the single
# source of the name it registers under -- the same name
# `resolve_enabled_tool_names` reads out of `tools.name`, and the same one
# `app/db/builtin_tools.py` seeds. A `tools` row naming something not listed
# here resolves to no Python class and is skipped (logged by
# `AgentRunner._resolve_specs`), exactly as a stale row always was.
#
# `SearchProductsTool`/`GetProductTool` (Task 5) are listed here for the
# same reason Phase 4's review made this tuple the thing it is: a `tools`
# row and a `DEFAULT_ENABLED_TOOL_NAMES` entry are both necessary but not
# sufficient -- without an entry here, `build_granted_registry` below can
# never construct the class, so a granted agent still could not call it.
# This is the one of Ruling 2's three required places that a migration
# cannot express at all; see `alembic/versions/0013_seed_product_tools.py`
# for the other two.
BUILTIN_TOOL_CLASSES: tuple[type[AgentTool], ...] = (
    RetrieveKnowledgeTool,
    CreateLeadTool,
    SearchProductsTool,
    GetProductTool,
)

# `LockedSessionTool`'s outer, registry-enforced budget (see its
# docstring): a soft, deliberately generous allowance for a call to sit
# queued behind however many sibling calls one step happens to gather,
# on top of the tool's own real timeout -- not a precise worst case, since
# nothing bounds how many calls one step can contain. A call still queued
# past its own budget plus this much is symptomatic of something
# structurally wrong (a leaked lock, a hung sibling that somehow evaded
# its own inner timeout), which is exactly the case this outer bound
# exists to still catch.
_LOCK_WAIT_BUDGET_SECONDS = 30.0

# How much longer than a tool's OWN budget the event-loop net in
# `LockedSessionTool._run_bounded` is allowed to run. Deliberately small,
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


def serialize_citations(citations: list[CitationPayload]) -> list[dict[str, Any]]:
    """`CitationPayload` -> a JSONB-/JSON-RPC-safe list of plain dicts.

    Not a plain `[c.model_dump() for c in citations]`: `CitationPayload`
    carries `uuid.UUID` fields, which neither `MessageToolCall.result`'s
    default JSON encoding nor an MCP `structured_content` payload can
    serialize on their own. Shared by `app/chat/service.py`'s
    `_serialize_tool_result` (for `MessageToolCall.result`) and, per
    `docs/PHASE-7.md` §5, the MCP server's own result shaping (`structured_content`
    carries `data` and these same citation fields) -- so the two callers can
    never render a citation differently.
    """
    return [
        {
            "chunk_id": str(c.chunk_id) if c.chunk_id is not None else None,
            "document_id": str(c.document_id) if c.document_id is not None else None,
            "product_id": str(c.product_id) if c.product_id is not None else None,
            "document_title": c.document_title,
            "rank": c.rank,
            "score": c.score,
            "excerpt": c.excerpt,
            "page": c.page,
        }
        for c in citations
    ]


class LockedSessionTool(AgentTool):
    """Wraps a Phase 4 builtin so every call serializes its use of the
    turn's shared session behind `lock`, instead of touching it with true,
    unguarded concurrency.

    This is the fix for a Critical finding in Task 7's first review round:
    `build_granted_registry` used to hand `RetrieveKnowledgeTool`/`CreateLeadTool`
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
                    **ctx.log_fields(),
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

        **What containment does and does not buy, stated exactly**, because
        it is easy to read the in-band `ChatError` as the whole story and it
        is not. In that residual case the client does get a terminal `error`
        event instead of a stream that stops mid-air -- but the session is
        still unusable, so the caller's own commit fails afterwards: for
        `POST /chat/stream` that is `tenant_session.__aexit__` raising inside
        `_pump`'s `finally`, after the terminal event is already on the wire,
        and `_queue_title` is skipped because the turn never committed. The
        turn's rows are gone either way. Containment converts "an uncaught
        exception the client sees as a generic internal error" into "a named
        failure the client can render", which is worth having and is not the
        same as saving the turn. Only prevention saves the turn, which is why
        `statement_timeout` is the primary fix and this is the floor.
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
                **ctx.log_fields(),
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
                **ctx.log_fields(),
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


async def resolve_enabled_tool_names(
    session: AsyncSession, tenant: TenantContext, agent_id: uuid.UUID
) -> list[str]:
    """Which tool names this agent may call, per `docs/ARCHITECTURE.md`
    §2.3's two-layer predicate: RLS on `session` is Layer 2, and the
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
            AgentToolLink.organization_id == tenant.organization_id,
            Tool.is_enabled.is_(True),
            Tool.type == ToolType.BUILTIN,
            or_(
                Tool.organization_id == tenant.organization_id,
                Tool.organization_id.is_(None),
            ),
        )
        .order_by(Tool.name, Tool.organization_id.is_(None))
    )
    rows = (await session.execute(stmt)).all()

    resolved = first_row_per_name(
        (name, bool(link_is_enabled)) for name, _org_id, link_is_enabled in rows
    )
    return [name for name, enabled in resolved.items() if enabled]


def build_granted_registry(session: AsyncSession, tool_names: Iterable[str]) -> ToolRegistry:
    """The builtins this agent is actually GRANTED -- and only those --
    registered fresh per turn, each wrapped in `LockedSessionTool`,
    sharing one `asyncio.Lock` created fresh here, so no two calls this
    turn ever touch `session` concurrently. See
    `LockedSessionTool`'s docstring for why a lock, not a session per
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

    Cheap: `ToolRegistry.register` does no I/O, and the tenant-leak
    check on each `args_model` is memoised per class in
    `app.tools.registry` -- so rebuilding the registry every turn
    costs a dict insert per granted tool, not a fresh reflective walk
    of its argument schema.
    """
    granted = set(tool_names)
    lock = asyncio.Lock()
    registry = ToolRegistry()
    for tool_cls in BUILTIN_TOOL_CLASSES:
        if tool_cls.name in granted:
            registry.register(LockedSessionTool(tool_cls, session, lock))
    return registry
