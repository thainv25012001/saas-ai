import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.service import AgentService
from app.conversations.schemas import AppendMessageInput, CreateConversationInput, RecordUsageInput
from app.conversations.service import ConversationService
from app.core.errors import AppError, NotFoundError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.db.models import (
    Agent,
    ConversationChannel,
    ConversationMessage,
    DocumentStatus,
    MessageCitation,
    MessageRole,
    Organization,
    UsageKind,
)
from app.documents.service import DocumentService
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.registry import get_provider
from app.llm.types import CompletionRequest, Usage
from app.llm.types import Message as LLMMessage
from app.prompts.context import assemble_context
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.service import PromptService
from app.rag.retrieve import RetrievalService, RetrievedChunk

logger = get_logger(__name__)

# A preview only -- the SSE `citations` payload deliberately does not carry
# the full chunk (see `ChatCitations`'s docstring below), so this bounds how
# much of it the UI gets instead. Long enough to recognise which passage
# matched at a glance, short enough that a handful of citations does not
# meaningfully add to the bytes of every `message_start` turn.
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


@dataclass(frozen=True, slots=True)
class ChatMessageStart:
    conversation_id: uuid.UUID
    message_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CitationPayload:
    """One retrieved passage's SSE-facing shape.

    Deliberately narrower than `RetrievedChunk`: `excerpt` is a short
    preview (see `_excerpt`), not the full chunk `content`, which would
    double the bytes of every grounded turn on the wire for no benefit the
    UI needs -- it already has `chunk_id` to fetch the rest on demand.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    rank: int
    score: float
    excerpt: str
    # `None` for a non-paginated source (plain text, Markdown, HTML) --
    # `RetrievedChunk.page` is `None` there too, since `_page_for_offset` in
    # `app/rag/chunk.py` only ever gets a page list from `extract()` for a
    # PDF. Absent is the honest state, not a value to fake as `1`.
    page: int | None


@dataclass(frozen=True, slots=True)
class ChatCitations:
    """Emitted after `ChatMessageStart` and before the first `ChatTextDelta`
    -- see `ChatService.send` -- so the UI can render sources while the
    answer is still streaming in, rather than only once the turn ends.
    """

    citations: list[CitationPayload]


@dataclass(frozen=True, slots=True)
class ChatTextDelta:
    text: str


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


ChatEvent = ChatMessageStart | ChatCitations | ChatTextDelta | ChatMessageEnd | ChatError


def _excerpt(content: str) -> str:
    if len(content) <= _EXCERPT_MAX_CHARS:
        return content
    return content[:_EXCERPT_MAX_CHARS].rstrip() + "..."


def _citation_payload(chunk: RetrievedChunk) -> CitationPayload:
    return CitationPayload(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        rank=chunk.rank,
        score=chunk.score,
        excerpt=_excerpt(chunk.content),
        page=chunk.page,
    )


class ChatService:
    """Turns an agent, its configured prompt, and a user message into a
    streamed reply that is persisted with usage and cost.

    See `docs/PHASE-2.md` §3, §5, §6 for the reasoning behind the ordering
    below; the short version is that every write goes through
    `ConversationService`/`AgentService`/`PromptService` rather than the ORM
    directly, because those are what close the FK-bypasses-RLS hole
    documented on `ConversationService.record_usage`.
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
        self._documents = DocumentService(session, tenant)

    async def send(
        self,
        agent_id: uuid.UUID,
        user_text: str,
        conversation_id: uuid.UUID | None = None,
        channel: ConversationChannel = ConversationChannel.API,
    ) -> AsyncIterator[ChatEvent]:
        # Step 1: load the agent and its config. Both raise NotFoundError
        # (cross-tenant, or a config row that does not exist) before any
        # conversation row is created -- a misconfigured or foreign agent_id
        # must never leave a partial conversation behind.
        agent = await self._agents.get_agent(agent_id)
        await self._agents.get_config(agent_id)

        # Resolve the provider before anything is written: a missing API key
        # (LLMConfigurationError) must surface as an operator problem, not
        # disguise itself as a conversation that was created and then failed.
        provider = self._provider_override or get_provider(agent.provider)

        system_prompt, prompt_version_id = await self._resolve_system_prompt(agent)

        # Step 4: create or load the conversation (404s cross-tenant for an
        # existing id, via ConversationService.get).
        if conversation_id is None:
            conversation = await self._conversations.create(
                agent_id, CreateConversationInput(channel=channel)
            )
        else:
            conversation = await self._conversations.get(conversation_id)

        # The assistant's message id is minted now, before its content is
        # known, so `ChatMessageStart` can tell the caller which message is
        # about to stream -- and the row persisted at the end (success or
        # failure) is created under this same id.
        message_id = uuid7()
        yield ChatMessageStart(conversation_id=conversation.id, message_id=message_id)

        # Grounding happens here: after ChatMessageStart (so the caller
        # already knows which message is coming) and before history/the
        # provider request are built (so a hit can still extend
        # `system_prompt` below). `retrieved_chunks` is threaded through to
        # the persistence step near the end of this method, in the same
        # transaction as the assistant message it grounded.
        retrieved_chunks = await self._retrieve_context(user_text)
        if retrieved_chunks:
            yield ChatCitations(citations=[_citation_payload(c) for c in retrieved_chunks])
            system_prompt = f"{system_prompt}\n\n{assemble_context(retrieved_chunks)}"

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
        # system prompt is already injected separately above, and a future
        # tool-role row (Phase 4) has no rendering defined here yet, so it is
        # skipped rather than guessed at.
        request_messages: list[LLMMessage] = []
        for row in history_rows:
            if row.role is MessageRole.USER:
                request_messages.append(LLMMessage.text("user", row.content or ""))
            elif row.role is MessageRole.ASSISTANT:
                request_messages.append(LLMMessage.text("assistant", row.content or ""))
        request_messages.append(LLMMessage.text("user", user_text))

        request = CompletionRequest(
            model=agent.model,
            messages=request_messages,
            system=system_prompt,
            max_tokens=agent.max_tokens,
            temperature=agent.temperature,
        )

        accumulated: list[str] = []
        usage = Usage()
        model_used = agent.model
        finish_reason: str | None = None
        chat_error: ChatError | None = None
        started_at = time.monotonic()

        try:
            # `StreamEvent` also declares `ErrorEvent`, deliberately unhandled
            # here: no provider today constructs one -- every provider
            # signals failure by *raising* (caught below as `AppError`), not
            # by yielding an in-band error event. If a future provider ever
            # signals failure that way instead, this loop would currently
            # treat the truncated stream as a normal success. Whoever adds
            # such a provider needs to add a branch here for it.
            async for event in provider.stream(request):
                if event.type == "text_delta":
                    accumulated.append(event.text)
                    yield ChatTextDelta(text=event.text)
                elif event.type == "message_end":
                    usage = event.usage
                    model_used = event.model
                    finish_reason = event.stop_reason
        except AppError as exc:
            chat_error = ChatError(code=exc.code, message=exc.message)

        latency_ms = int((time.monotonic() - started_at) * 1000)
        final_text = "".join(accumulated)

        if chat_error is None:
            cost = estimate_cost(model_used, usage)
            await self._conversations.append_message(
                conversation.id,
                AppendMessageInput(
                    id=message_id,
                    role=MessageRole.ASSISTANT,
                    content=final_text,
                    prompt_version_id=prompt_version_id,
                    provider=agent.provider,
                    model=model_used,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    finish_reason=finish_reason,
                ),
            )
            # Same transaction as the assistant message above. Unlike
            # `record_usage` (success-only, just below -- there is no
            # reliable token count for a stream that never reached
            # `message_end`), citations are recorded on *both* branches of
            # this split: see the matching call and comment in the `else`
            # below for why that billing-shaped reasoning does not transfer
            # here.
            await self._record_citations(message_id, retrieved_chunks)
            await self._conversations.record_usage(
                RecordUsageInput(
                    agent_id=agent.id,
                    conversation_id=conversation.id,
                    kind=UsageKind.LLM,
                    provider=agent.provider,
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
                    provider=agent.provider,
                    model=model_used,
                    latency_ms=latency_ms,
                    error=chat_error.message,
                ),
            )
            # Recorded here too, not just on the success path above: the
            # assistant message this turn produced -- partial and marked
            # `error=`, but persisted -- was still generated from a prompt
            # that included these chunks, and per docs/PHASE-3.md §5 this
            # table's whole reason for existing is to make "did it answer
            # from the sources?" answerable after the fact. A message with
            # zero citations here would be indistinguishable from one that
            # was never grounded at all, which is exactly the ambiguity
            # this table exists to remove.
            await self._record_citations(message_id, retrieved_chunks)
            yield chat_error

    async def _retrieve_context(self, query: str) -> list[RetrievedChunk]:
        """Grounds this turn in the organization's own documents when there
        is a corpus to ground it in.

        An organization with none must behave exactly as it did before this
        task existed -- no query against `document_chunks`, no citations
        event -- which is why this checks for a ready document first rather
        than simply calling `RetrievalService` and trusting an empty result
        to look the same as "never asked": a retrieval call that happens to
        find nothing is not "no retrieval call".

        A failure past that point is caught, not propagated: a retrieval
        outage (or a failure in the readiness check itself) must cost the
        user grounding, not the answer they came here for. See
        `docs/ARCHITECTURE.md` §2.3 for why `self._documents` -- not a raw
        query -- is what does that check: it already carries the explicit
        `organization_id` filter this codebase requires alongside RLS.

        The readiness check and `RetrievalService.retrieve` both run raw SQL
        on *this* session (see `app/rag/retrieve.py`) -- the same session
        `send()` goes on to use for history, `append_message`, and
        `record_usage`. Postgres aborts the whole surrounding transaction on
        any statement error, not just the failing one, so a bare
        `try/except` around a plain `await` is not enough: catching the
        exception stops it from propagating, but every later statement on
        this session would still raise `InFailedSQLTransactionError` against
        the poisoned transaction, and the user would lose their entire turn
        -- the opposite of "degrade, not break". `begin_nested()` opens a
        SAVEPOINT for this block; a failure inside it rolls back only to
        that savepoint (SQLAlchemy does this automatically when an
        exception propagates out of the `async with`), leaving the outer
        transaction -- and every statement `send()` runs after this
        returns -- perfectly usable. This is not hypothetical: a single
        chunk embedded at the wrong dimension makes `embedding <=>
        :query_vector` fail this way for every future query against that
        row, and a statement timeout or a momentarily-unavailable `vector`
        extension would too.
        """
        try:
            async with self.session.begin_nested():
                ready_documents = await self._documents.list_documents(
                    status=DocumentStatus.READY, limit=1
                )
                if not ready_documents:
                    return []
                chunks = await RetrievalService(self.session, self.tenant).retrieve(query)
        except Exception:
            logger.warning("rag_retrieval_failed", exc_info=True)
            return []

        if not chunks:
            # An organization with a ready corpus whose every turn retrieves
            # nothing is the shape a misconfigured relevance floor takes --
            # or an embedding model swapped without re-embedding, where the
            # stored vectors and the query vector no longer share a space.
            # Neither raises, and without this line neither leaves a trace.
            # The query text is not logged: it is the customer's own words.
            logger.info(
                "rag_retrieval_empty",
                organization_id=str(self.tenant.organization_id),
                query_chars=len(query),
            )
        return chunks

    async def _record_citations(self, message_id: uuid.UUID, chunks: list[RetrievedChunk]) -> None:
        """Writes one `MessageCitation` row per retrieved chunk, in the
        caller's open transaction.

        The scoped `SELECT` on `message_id` below is load-bearing, not
        belt-and-braces -- identical in shape and reason to
        `ConversationService.append_message`'s own ownership check: a
        Postgres FK constraint check runs with elevated privileges and does
        not consult this session's RLS policy, so an INSERT here would
        happily attach a citation to another tenant's `message_id` even
        though a plain SELECT under this session's RLS sees zero rows for
        it. In `ChatService.send`'s own call path `message_id` is always one
        this same call just minted for this same tenant, so this specific
        check can never actually fire today -- but the FK-bypass hazard is a
        property of the table, not of today's one caller, and this is what
        keeps that true regardless of who calls this method next.

        `chunk_id`/`document_id` need no equivalent check: both come
        straight out of `RetrievalService.retrieve`, which is itself
        two-layer tenant-scoped (see `app/rag/retrieve.py`), so a value
        reaching here has already been proven to belong to this
        organization.

        `document_title` and `excerpt` are written alongside those ids
        rather than left to a join, because the ids are `ON DELETE SET
        NULL`: a re-ingest or a document delete nulls them out and the
        citation has to stay legible on its own. See `MessageCitation`'s
        own docstring.
        """
        if not chunks:
            return

        result = await self.session.execute(
            select(ConversationMessage.id).where(
                ConversationMessage.id == message_id,
                ConversationMessage.organization_id == self.tenant.organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("message not found")

        self.session.add_all(
            [
                MessageCitation(
                    id=uuid7(),
                    organization_id=self.tenant.organization_id,
                    message_id=message_id,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    # Denormalised deliberately: both ids above are
                    # `ON DELETE SET NULL`, so a re-ingest or a
                    # `deleteDocument` leaves this row with nothing to join
                    # to. These two columns are what still say *what* was
                    # cited afterwards. `_excerpt` is the same preview the
                    # SSE `citations` event carries, for the same reason it
                    # is a preview there: enough to recognise the passage,
                    # not a second copy of the corpus.
                    document_title=chunk.document_title,
                    excerpt=_excerpt(chunk.content),
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
