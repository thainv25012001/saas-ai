import enum
import uuid
from datetime import datetime

import strawberry

from app.core.errors import AuthenticationError
from app.db.models import Agent as AgentModel
from app.db.models import AgentConfig as AgentConfigModel
from app.db.models import Document as DocumentModel
from app.db.models import Organization as OrganizationModel
from app.db.models import Prompt as PromptModel
from app.db.models import PromptVersion as PromptVersionModel
from app.graphql.context import Context


@strawberry.enum
class AgentStatus(enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


@strawberry.type
class ModelOption:
    """One entry in the dashboard's model picker. `context_length` is `None`
    when the provider does not publish one."""

    id: str
    label: str
    context_length: int | None


@strawberry.type
class ProviderInfo:
    """One entry in the dashboard's provider dropdown.

    `configured` is whether the server holds the API key this provider needs,
    which is the same condition `app.llm.registry._build` raises
    `LLMConfigurationError` on. The dashboard greys out the rest: an agent
    pointed at a provider with no key cannot answer, and discovering that in
    the playground rather than at the dropdown wastes the user's time.

    Only the flag, never the key or any part of it -- this is answered for any
    authenticated member of any tenant.
    """

    id: str
    configured: bool


@strawberry.type
class Me:
    user_id: uuid.UUID
    email: str
    full_name: str
    organization_id: uuid.UUID
    organization_name: str
    role: str


@strawberry.type
class Organization:
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    created_at: datetime

    @classmethod
    def from_model(cls, model: OrganizationModel) -> "Organization":
        return cls(
            id=model.id,
            name=model.name,
            slug=model.slug,
            plan=model.plan,
            created_at=model.created_at,
        )


@strawberry.type
class AgentConfig:
    id: uuid.UUID
    tone: str
    language: str
    persona: str | None
    greeting: str | None
    fallback_message: str
    enabled_tool_names: list[str]
    retrieval_top_k: int
    retrieval_min_score: float
    max_agent_steps: int

    @classmethod
    def from_model(cls, model: AgentConfigModel) -> "AgentConfig":
        return cls(
            id=model.id,
            tone=model.tone,
            language=model.language,
            persona=model.persona,
            greeting=model.greeting,
            fallback_message=model.fallback_message,
            enabled_tool_names=list(model.enabled_tool_names),
            retrieval_top_k=model.retrieval_top_k,
            retrieval_min_score=model.retrieval_min_score,
            max_agent_steps=model.max_agent_steps,
        )


@strawberry.type
class Agent:
    id: uuid.UUID
    name: str
    slug: str
    status: AgentStatus
    provider: str
    model: str
    temperature: float
    max_tokens: int
    prompt_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, model: AgentModel) -> "Agent":
        return cls(
            id=model.id,
            name=model.name,
            slug=model.slug,
            status=AgentStatus(model.status.value),
            provider=model.provider,
            model=model.model,
            temperature=model.temperature,
            max_tokens=model.max_tokens,
            prompt_id=model.prompt_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @strawberry.field
    async def config(self, info: strawberry.Info[Context, None]) -> "AgentConfig | None":
        # An unauthenticated request builds a Context with no session, so
        # there is no dataloader to batch through. `agents { config }` on
        # such a request must still fail as a clean `unauthenticated`
        # GraphQL error (raised by the `agents`/`agent` resolver itself,
        # before this field ever runs) rather than an AttributeError here.
        if info.context.config_loader is None:
            raise AuthenticationError("authentication required")
        model = await info.context.config_loader.load(self.id)
        return AgentConfig.from_model(model) if model else None


@strawberry.type
class PromptVersion:
    id: uuid.UUID
    version: int
    system_prompt: str
    is_active: bool
    notes: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: PromptVersionModel) -> "PromptVersion":
        return cls(
            id=model.id,
            version=model.version,
            system_prompt=model.system_prompt,
            is_active=model.is_active,
            notes=model.notes,
            created_at=model.created_at,
        )


@strawberry.type
class Prompt:
    id: uuid.UUID
    name: str
    key: str
    description: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: PromptModel) -> "Prompt":
        return cls(
            id=model.id,
            name=model.name,
            key=model.key,
            description=model.description,
            created_at=model.created_at,
        )


@strawberry.enum
class DocumentStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


@strawberry.type
class Document:
    id: uuid.UUID
    title: str
    status: DocumentStatus
    mime_type: str | None
    file_size: int | None
    checksum: str | None
    error: str | None
    created_at: datetime
    processed_at: datetime | None

    @classmethod
    def from_model(cls, model: DocumentModel) -> "Document":
        return cls(
            id=model.id,
            title=model.title,
            status=DocumentStatus(model.status.value),
            mime_type=model.mime_type,
            file_size=model.file_size,
            checksum=model.checksum,
            error=model.error,
            created_at=model.created_at,
            processed_at=model.processed_at,
        )

    @strawberry.field
    async def chunk_count(self, info: strawberry.Info[Context, None]) -> int:
        # Same reasoning as Agent.config above: an unauthenticated request
        # builds a Context with no loader at all, and that must fail as a
        # clean `unauthenticated` error from the `documents`/`document`
        # resolver itself, before this field ever runs -- not an
        # AttributeError here.
        if info.context.chunk_count_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.chunk_count_loader.load(self.id)


@strawberry.input
class CreateAgentInput:
    """`provider` and `model` are non-null and have no default, matching
    `app.agents.schemas.CreateAgentInput`.

    Both used to be nullable and were resolved server-side from
    `DEFAULT_LLM_PROVIDER` when omitted. This is the only path a real user
    creates an agent through, and the dashboard sent neither — so every agent
    anyone made landed on `fake`, the offline provider that answers with a
    canned reply, and they had to find that out and fix it afterwards.

    Non-null here rather than only in the pydantic schema because this is what
    the dashboard's codegen reads: nullable fields let the web app compile a
    `createAgent` call that omits them, which is exactly how this happened.

    `UpdateAgentInput` below stays nullable on purpose — there `None` means
    "leave unchanged", so renaming an agent must not require restating its
    model.
    """

    name: str
    provider: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 1024


@strawberry.input
class UpdateAgentInput:
    name: str | None = None
    status: AgentStatus | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@strawberry.input
class UpdateAgentConfigInput:
    persona: str | None = None
    tone: str | None = None
    language: str | None = None
    greeting: str | None = None
    fallback_message: str | None = None
    enabled_tool_names: list[str] | None = None
    retrieval_top_k: int | None = None
    retrieval_min_score: float | None = None
    max_agent_steps: int | None = None


@strawberry.input
class CreatePromptInput:
    name: str
    key: str
    system_prompt: str
    description: str | None = None


@strawberry.input
class CreatePromptVersionInput:
    system_prompt: str
    notes: str | None = None
