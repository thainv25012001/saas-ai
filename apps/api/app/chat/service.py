import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
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
from app.tools.base import ToolContext, ToolResult
from app.tools.leads import CreateLeadTool
from app.tools.registry import ToolRegistry
from app.tools.retrieve import RetrieveKnowledgeTool

logger = get_logger(__name__)

# A preview only -- the SSE `citations` payload deliberately does not carry
# the full chunk (see `ChatCitations`'s docstring below), and `tool_call_end`
# deliberately does not carry a tool's full result payload either, for the
# same reason -- a tool result can be a multi-kilobyte assembled context
# block (`assemble_context`'s whole output, for `retrieve_knowledge`), and
# shipping that twice per turn (once as the model's own context, once again
# over SSE for a UI card) buys nothing the UI needs beyond "did it work, and
# roughly what came back". Mirrors `app/rag/retrieve.py`'s own
# `_EXCERPT_MAX_CHARS`/`_excerpt` (used there for `CitationPayload.excerpt`)
# rather than importing that module's private helper: the two operate on
# different inputs (a tool's plain-text result vs. a `RetrievedChunk`) and
# keeping this one local avoids a chat-module -> rag-module dependency for a
# single integer and a slice.
_EXCERPT_MAX_CHARS = 240

# PHASE-2.md §6: "the last `history_window` turns (config, default 20)".
# There is no dedicated schema column for this yet (see `agent_configs` in
# ARCHITECTURE.md §3.2) -- Phase 2 has no UI to set one -- so the knob lives
# on the service that consumes it, exactly like `provider_override` on the
# same class already does for a value that otherwise resolves from data.
DEFAULT_HISTORY_WINDOW = 20

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


def _excerpt(text: str) -> str:
    if len(text) <= _EXCERPT_MAX_CHARS:
        return text
    return text[:_EXCERPT_MAX_CHARS].rstrip() + "..."


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
    `ToolResult.content` (see `_excerpt`), never the full payload -- the
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
        registry = self._build_registry()
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
                            _ToolCallRecord(
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
                                result=_excerpt(result.result.content),
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
            yield ChatMessageEnd(
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
            yield chat_error

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

    def _build_registry(self) -> ToolRegistry:
        """Every Phase 4 builtin, registered fresh per turn.

        Cheap: `ToolRegistry.register` does no I/O, only the
        tenant-leak-check on each `args_model` (already paid once per class
        at import time in practice, since Python caches the class object --
        this just re-runs it). Both tools are handed `self.session` --
        the caller's own, already tenant-bound session -- not one either
        tool opens for itself, so their own `begin_nested()` savepoints (see
        each tool's docstring) protect *this* turn's surrounding
        transaction.
        """
        registry = ToolRegistry()
        registry.register(RetrieveKnowledgeTool(self.session))
        registry.register(CreateLeadTool(self.session))
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
                    rank=chunk.rank,
                    score=chunk.score,
                )
                for chunk in chunks
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
