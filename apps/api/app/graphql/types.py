import enum
import json
import uuid
from datetime import datetime
from typing import Any

import strawberry

from app.core.errors import AuthenticationError
from app.db.models import Agent as AgentModel
from app.db.models import AgentConfig as AgentConfigModel
from app.db.models import Conversation as ConversationModel
from app.db.models import ConversationMessage as MessageModel
from app.db.models import Document as DocumentModel
from app.db.models import Lead as LeadModel
from app.db.models import MessageCitation as MessageCitationModel
from app.db.models import Organization as OrganizationModel
from app.db.models import Product as ProductModel
from app.db.models import ProductImport as ProductImportModel
from app.db.models import Prompt as PromptModel
from app.db.models import PromptVersion as PromptVersionModel
from app.db.models import Tool as ToolModel
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


@strawberry.enum
class ConversationChannel(enum.Enum):
    PLAYGROUND = "playground"
    WIDGET = "widget"
    API = "api"


@strawberry.enum
class ConversationStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


@strawberry.enum
class MessageRole(enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@strawberry.type
class MessageCitation:
    id: uuid.UUID
    chunk_id: uuid.UUID | None
    document_id: uuid.UUID | None
    #: Untrusted -- the uploaded document's own title, never escaped by the
    #: server. Rendering it must go through JSX text interpolation only; see
    #: the same warning on `Citation` in `apps/web/src/lib/sse.ts`.
    document_title: str
    excerpt: str
    rank: int
    score: float

    @classmethod
    def from_model(cls, model: MessageCitationModel) -> "MessageCitation":
        return cls(
            id=model.id,
            chunk_id=model.chunk_id,
            document_id=model.document_id,
            document_title=model.document_title,
            excerpt=model.excerpt,
            rank=model.rank,
            score=model.score,
        )


@strawberry.type
class Message:
    id: uuid.UUID
    seq: int
    role: MessageRole
    #: Nullable, and deliberately surfaced as such: a turn that died before
    #: producing text has no content and an `error`, and the transcript
    #: renders exactly that rather than pretending the turn never happened.
    content: str | None
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    #: A string, not a float: `Decimal` is what the column holds precisely,
    #: and a float would round a fraction of a cent away on the wire. The
    #: streaming `message_end` event already sends cost as a string for the
    #: same reason.
    cost_usd: str | None
    latency_ms: int | None
    finish_reason: str | None
    error: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: MessageModel) -> "Message":
        return cls(
            id=model.id,
            seq=model.seq,
            role=MessageRole(model.role.value),
            content=model.content,
            provider=model.provider,
            model=model.model,
            input_tokens=model.input_tokens,
            output_tokens=model.output_tokens,
            cost_usd=None if model.cost_usd is None else str(model.cost_usd),
            latency_ms=model.latency_ms,
            finish_reason=model.finish_reason,
            error=model.error,
            created_at=model.created_at,
        )

    @strawberry.field
    async def citations(self, info: strawberry.Info[Context, None]) -> list[MessageCitation]:
        # Batched through a loader for the same reason `Agent.config` is:
        # resolved field-by-field, a 40-message transcript would issue 40
        # queries against `message_citations`.
        if info.context.citation_loader is None:
            raise AuthenticationError("authentication required")
        rows = await info.context.citation_loader.load(self.id)
        return [MessageCitation.from_model(row) for row in rows]


@strawberry.type
class Conversation:
    id: uuid.UUID
    agent_id: uuid.UUID
    channel: ConversationChannel
    status: ConversationStatus
    #: `None` until the title job has run -- the list falls back to the
    #: conversation's first message, so a row is never blank.
    title: str | None
    last_message_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: ConversationModel) -> "Conversation":
        return cls(
            id=model.id,
            agent_id=model.agent_id,
            channel=ConversationChannel(model.channel.value),
            status=ConversationStatus(model.status.value),
            title=model.title,
            last_message_at=model.last_message_at,
            created_at=model.created_at,
        )

    @strawberry.field
    async def preview(self, info: strawberry.Info[Context, None]) -> str | None:
        """The first question asked, for a list row whose title has not landed
        yet -- the title job is asynchronous, and a brand-new conversation is
        exactly the one someone is looking at."""
        if info.context.preview_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.preview_loader.load(self.id)

    @strawberry.field
    async def messages(self, info: strawberry.Info[Context, None]) -> list[Message]:
        # Batched, like `preview` above and `citations` on `Message`: fetched
        # per conversation, `conversations { messages }` would be a query per
        # row. Unbounded by design -- a limit exists to bound what is sent to
        # the model as context, and someone reading their own history is not
        # paying for it as tokens.
        if info.context.messages_loader is None:
            raise AuthenticationError("authentication required")
        rows = await info.context.messages_loader.load(self.id)
        return [Message.from_model(row) for row in rows]


@strawberry.enum
class LeadStatus(enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


@strawberry.type
class Lead:
    id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: uuid.UUID
    #: Untrusted -- captured from the model's own `create_lead` tool call,
    #: which in turn copied whatever the visitor typed. Render as JSX text
    #: only, exactly like `Message.content` and `MessageCitation`'s fields
    #: above: no `dangerouslySetInnerHTML`, no markdown pass.
    name: str | None
    email: str | None
    phone: str | None
    interest: str | None
    status: LeadStatus
    created_at: datetime

    @classmethod
    def from_model(cls, model: LeadModel) -> "Lead":
        return cls(
            id=model.id,
            agent_id=model.agent_id,
            conversation_id=model.conversation_id,
            name=model.name,
            email=model.email,
            phone=model.phone,
            interest=model.interest,
            status=LeadStatus(model.status.value),
            created_at=model.created_at,
        )

    @strawberry.field
    async def conversation(self, info: strawberry.Info[Context, None]) -> "Conversation | None":
        """The thread this lead came from, for the dashboard's "captured
        during this conversation" column. Batched through a loader for the
        same reason `Agent.config` is: a page of leads would otherwise issue
        one query per row just to show a title.

        Nullable in principle (the conversation could have been deleted
        since -- `ON DELETE CASCADE` on `leads.conversation_id` actually
        takes the lead with it, but a resolver reading through a loader
        keyed by id has no way to assume that stays true forever), so this
        mirrors `Query.conversation`'s own "not found is null" contract
        rather than raising.
        """
        if info.context.conversation_loader is None:
            raise AuthenticationError("authentication required")
        model = await info.context.conversation_loader.load(self.conversation_id)
        return Conversation.from_model(model) if model else None


@strawberry.type
class AgentTool:
    """One row of the per-agent tool toggle (Task 8) -- a `tools` row this
    organization can see (global builtin or its own), plus whether *this*
    agent's `agent_tools` link enables it. See `AgentService.list_tools` for
    what `is_enabled` means when no link exists at all.
    """

    id: uuid.UUID
    name: str
    description: str | None
    is_enabled: bool

    @classmethod
    def from_pair(cls, tool: ToolModel, is_enabled: bool) -> "AgentTool":
        return cls(id=tool.id, name=tool.name, description=tool.description, is_enabled=is_enabled)


@strawberry.enum
class ProductAvailability(enum.Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    DISCONTINUED = "discontinued"


@strawberry.enum
class ProductSearchIndex(enum.Enum):
    """Whether the agent's semantic search can find this product by meaning
    -- `app.products.embedding.needs_reembedding`'s three states, named for
    the dashboard. Keyword and structured search reach every row regardless;
    only the vector arm depends on this."""

    #: Embedded, from the text the row has now.
    INDEXED = "indexed"
    #: Never embedded (`embedding_source_hash IS NULL`): the vector arm
    #: cannot match it at all.
    NOT_INDEXED = "not_indexed"
    #: Embedded from text the row no longer has (`embedding_stale`): the
    #: vector arm ranks it on a description that is out of date.
    STALE = "stale"


def _search_index(model: ProductModel) -> ProductSearchIndex:
    if model.embedding_source_hash is None:
        return ProductSearchIndex.NOT_INDEXED
    if model.embedding_stale:
        return ProductSearchIndex.STALE
    return ProductSearchIndex.INDEXED


@strawberry.type
class ProductAttribute:
    """One `attributes` entry. Untrusted in both halves -- the key and the
    value are each whatever a customer's CSV/JSON said."""

    key: str
    value: str


def _attribute_value(value: Any) -> str:
    """A string arrives as itself; anything else (a number, a bool, a nested
    list or object) as its JSON text. Flattened here so the dashboard
    receives one shape and renders it as text, instead of walking an
    arbitrary customer-shaped object client-side."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


@strawberry.type
class Product:
    id: uuid.UUID
    external_id: str
    #: Untrusted -- `name`, `description`, `category` and every
    #: `attributes` key and value come straight from a customer's uploaded
    #: catalogue with no server-side escaping. Render as JSX text only: no
    #: `dangerouslySetInnerHTML`, no markdown pass (docs/DESIGN.md
    #: "Untrusted text, beyond citations").
    name: str
    description: str | None
    category: str | None
    #: A string, not a float, for the reason `Message.cost_usd` is one:
    #: `Numeric(12, 2)` is exact and a float is not.
    price: str | None
    currency: str | None
    attributes: list[ProductAttribute]
    availability: ProductAvailability
    stock_quantity: int | None
    is_active: bool
    search_index: ProductSearchIndex
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, model: ProductModel) -> "Product":
        return cls(
            id=model.id,
            external_id=model.external_id,
            name=model.name,
            description=model.description,
            category=model.category,
            price=None if model.price is None else str(model.price),
            currency=model.currency,
            attributes=[
                ProductAttribute(key=key, value=_attribute_value(value))
                for key, value in model.attributes.items()
            ],
            availability=ProductAvailability(model.availability.value),
            stock_quantity=model.stock_quantity,
            is_active=model.is_active,
            search_index=_search_index(model),
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


@strawberry.enum
class ProductImportStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@strawberry.type
class ProductImportRowError:
    """One failed row. `row` is Task 3's human-visible number -- 1-based and
    header-aware for a CSV, so it is the line a customer would point at in
    their own spreadsheet. `external_id` and `message` may quote the row's
    own content, so they are untrusted like `Product`'s fields."""

    row: int
    external_id: str | None
    message: str


# Ceiling on `ProductImport.errors`' `limit`. A 20 MB file of bad rows can
# record tens of thousands of row errors, and the dashboard re-reads the
# import list on a poll -- `failed_count` already carries the total.
_MAX_IMPORT_ERRORS = 200


@strawberry.type
class ProductImport:
    id: uuid.UUID
    filename: str | None
    status: ProductImportStatus
    #: `None` until parsing finishes, alongside the two counts below.
    total_rows: int | None
    succeeded_count: int
    failed_count: int
    #: A whole-file failure (bad JSON, a missing required column), set
    #: instead of any per-row error.
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    row_errors: strawberry.Private[list[dict[str, Any]]]

    @classmethod
    def from_model(cls, model: ProductImportModel) -> "ProductImport":
        return cls(
            id=model.id,
            filename=model.filename,
            status=ProductImportStatus(model.status.value),
            total_rows=model.total_rows,
            succeeded_count=model.succeeded_count,
            failed_count=model.failed_count,
            error=model.error,
            created_at=model.created_at,
            completed_at=model.completed_at,
            row_errors=model.errors,
        )

    @strawberry.field
    def errors(self, limit: int = 50) -> list[ProductImportRowError]:
        """The first `limit` failed rows, by row number. Sorted here because
        the importer records them by the phase that caught them (parse, then
        duplicate, then write), not by position in the file. Bounded because
        the list is unbounded in the database; `failed_count` says how many
        there are in all."""
        limit = max(0, min(limit, _MAX_IMPORT_ERRORS))
        return [
            ProductImportRowError(
                row=entry["row"], external_id=entry.get("external_id"), message=entry["message"]
            )
            for entry in sorted(self.row_errors, key=lambda entry: entry["row"])[:limit]
        ]


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
