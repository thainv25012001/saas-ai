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
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import (
    Agent,
    ConversationChannel,
    MessageRole,
    Organization,
    UsageKind,
)
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.registry import get_provider
from app.llm.types import CompletionRequest, Usage
from app.llm.types import Message as LLMMessage
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.service import PromptService

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


ChatEvent = ChatMessageStart | ChatTextDelta | ChatMessageEnd | ChatError


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
            yield chat_error

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
