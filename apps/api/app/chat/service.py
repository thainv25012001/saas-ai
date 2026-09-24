import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import (
    AgentFinish,
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
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.db.models import (
    Agent,
    ConversationChannel,
    ConversationMessage,
    MessageCitation,
    MessageRole,
    MessageToolCall,
    Organization,
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
from app.tools.base import ToolContext, ToolResult
from app.tools.runtime import (
    build_granted_registry,
    resolve_enabled_tool_names,
    serialize_citations,
)

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

# Yielded in place of a `ChatMessageEnd` when the turn's own persistence
# fails. Deliberately generic, and deliberately the same shape every other
# in-band failure uses: the user has already seen the answer stream, and the
# only honest thing left to say is that it was not saved. The real exception
# goes to the log, never to the wire.
_PERSISTENCE_FAILED_MESSAGE = (
    "The assistant answered, but this turn could not be saved. Please try again."
)


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

#: `messages.finish_reason` is `String(50)` (`app/db/models/conversation.py`),
#: and a provider's `stop_reason` is as model-derived as anything else here.
_FINISH_REASON_MAX_CHARS = 50


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
    default JSON encoding cannot serialize on its own. `serialize_citations`
    (`app/tools/runtime.py`) does the by-hand conversion, shared with the MCP
    server's own result shaping (`docs/PHASE-7.md` §5) rather than teaching
    `CitationPayload` a custom serializer -- this and MCP's `structured_content`
    are the two places a `CitationPayload` needs to become a plain dict for
    storage/transport rather than round-trip through Pydantic.
    """
    return {
        "content": result.content,
        "data": result.data,
        "citations": serialize_citations(result.citations),
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
        prompt_version_id: uuid.UUID | None = None,
    ) -> AsyncIterator[ChatEvent]:
        """`override_provider`/`override_model` answer this one turn with
        something other than the agent's configured pair, without writing
        anything back to the agent -- what the playground's model picker sends
        so a model can be tried before it is committed to. Distinct from
        `provider_override` on `__init__`, which injects a whole `LLMProvider`
        object (tests, and nothing else); these are the *names* a caller may
        pass per request.

        `prompt_version_id`, left `None`, changes nothing: the agent's active
        version answers, or `DEFAULT_SALES_SYSTEM_PROMPT` when the agent has
        no prompt at all -- exactly as before this parameter existed. Given a
        value, PHASE-6.md §5's evaluation run pins a turn to that exact
        version instead: its text and declared variables are what render, and
        the id (not the active version's) is what `ChatMessageEnd` and the
        persisted assistant message record -- so a version activated while a
        run is in progress cannot change what an already-pinned turn used.
        The version must satisfy `version.prompt_id == agent.prompt_id`,
        checked in `_resolve_system_prompt`; an agent with no prompt at all
        given a version is the same mismatch. Both raise `ValidationError`,
        resolved in the same pre-write span the provider is (below): a bad
        pin is a caller mistake, not a reason to leave a conversation behind.
        `app/api/chat.py` never passes this -- the playground always runs the
        agent's own active version.
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

        # A bad pin (wrong prompt, or an agent with none) is validated in the
        # same pre-write span as the provider above, for the same reason: the
        # caller's own resolved id is consumed here and replaced by what
        # actually answered the turn -- `None` for the fallback default,
        # otherwise whichever version's text was rendered.
        system_prompt, prompt_version_id = await self._resolve_system_prompt(
            agent, prompt_version_id
        )

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
        registry = build_granted_registry(self.session, tool_names)
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
        # The provider's own `stop_reason` for the step that ended the turn,
        # via `AgentFinish`. `None` until one arrives, and `None` afterwards
        # if the provider sent none -- "it did not say", which is a different
        # fact from the `None` this column used to carry unconditionally.
        stop_reason: str | None = None
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
                elif isinstance(event, AgentFinish):
                    stop_reason = event.stop_reason
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
                        # `step_limit_reached` wins: the loop's own verdict
                        # is the more important fact, and the provider's
                        # `stop_reason` for the last step it managed to run
                        # ("tool_use") would be actively misleading there.
                        # `String(50)`, so a provider inventing a long one is
                        # truncated rather than allowed to fail the write --
                        # the same rule as `_ToolCallRecord.build`.
                        finish_reason=(
                            "step_limit_reached"
                            if step_limit_hit
                            else _strip_nulls(stop_reason)[:_FINISH_REASON_MAX_CHARS]
                            if stop_reason
                            else None
                        ),
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
            # `statement_timeout`, see
            # `app.tools.runtime.LockedSessionTool._run_bounded`) is what
            # stops the session being lost in the first place.
            logger.exception(
                "chat_turn_persistence_failed",
                agent_id=str(agent_id),
                conversation_id=str(conversation.id),
                message_id=str(message_id),
            )
            final_event = ChatError(code="internal_error", message=_PERSISTENCE_FAILED_MESSAGE)

        yield final_event

    async def _resolve_enabled_tool_names(self, agent_id: uuid.UUID) -> list[str]:
        """Thin wrapper kept for existing tests that call this directly
        (`ChatService(...)._resolve_enabled_tool_names(agent_id)`). The
        implementation moved to `app.tools.runtime.resolve_enabled_tool_names`
        as part of Phase 7 Task 1 ("one tool runtime, two callers",
        `docs/PHASE-7.md` §5) -- see that function's docstring for the
        two-layer predicate and the shadowing rule it implements. No logic
        lives here; this only supplies `self.session`/`self.tenant`.
        """
        return await resolve_enabled_tool_names(self.session, self.tenant, agent_id)

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
        `app.tools.runtime.LockedSessionTool.execute`, which times the whole
        call (including any wait for a sibling call holding the shared
        session's lock), not measured here.
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
        `product_id` (Task 5) is the identical argument, one level down: it
        comes straight out of `ProductSearchService`/`ProductService` (via
        `app/tools/products.py`), both two-layer tenant-scoped in exactly
        the same way, so a citation naming a product has already been
        proven to belong to this organization before it ever reaches here.

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
                    product_id=chunk.product_id,
                    document_title=chunk.document_title,
                    excerpt=chunk.excerpt,
                    rank=rank,
                    score=chunk.score,
                )
                for rank, chunk in enumerate(chunks, start=1)
            ]
        )
        await self.session.flush()

    async def _resolve_system_prompt(
        self, agent: Agent, pinned_version_id: uuid.UUID | None = None
    ) -> tuple[str, uuid.UUID | None]:
        """Step 2+3: resolve the prompt version that answers this turn (a
        pinned one, else the active one, else the default) and render it.
        `prompt_version_id` is `None` exactly when the fallback default was
        used -- that is what lets every assistant message be traced back to
        the exact prompt text that produced it, per PHASE-2.md §6, without
        inventing a version id for text that has none.

        `pinned_version_id` (Phase 6, PHASE-6.md §5) is validated here,
        before any conversation row exists (`send` calls this in the same
        pre-write span it resolves the provider in): `get_version` itself
        raises `NotFoundError` for a cross-tenant id, and the ownership check
        below raises `ValidationError` for a version that belongs to some
        OTHER prompt of this same organization, or to an agent with no
        prompt at all (`agent.prompt_id is None` can equal no real
        `version.prompt_id`). A pin that passes both checks skips the active-
        version lookup entirely -- it answers with that exact version's text
        regardless of which version is active right now.
        """
        declared_variables: dict[str, str]
        if pinned_version_id is not None:
            version = await self._prompts.get_version(pinned_version_id)
            if version.prompt_id != agent.prompt_id:
                raise ValidationError("prompt version does not belong to this agent's prompt")
            template = version.system_prompt
            declared_variables = {k: str(v) for k, v in version.variables.items()}
            prompt_version_id: uuid.UUID | None = version.id
        elif agent.prompt_id is not None:
            version = await self._prompts.active_version(agent.prompt_id)
            template = version.system_prompt
            declared_variables = {k: str(v) for k, v in version.variables.items()}
            prompt_version_id = version.id
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
