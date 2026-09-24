import enum
import json
import uuid
from datetime import datetime
from typing import Any

import strawberry

from app.api_keys.service import CreatedApiKey as CreatedApiKeyResult
from app.core.errors import AuthenticationError
from app.db.models import Agent as AgentModel
from app.db.models import AgentConfig as AgentConfigModel
from app.db.models import ApiKey as ApiKeyModel
from app.db.models import Conversation as ConversationModel
from app.db.models import ConversationMessage as MessageModel
from app.db.models import Document as DocumentModel
from app.db.models import EvalCase as EvalCaseModel
from app.db.models import EvalDataset as EvalDatasetModel
from app.db.models import EvalResult as EvalResultModel
from app.db.models import EvalRun as EvalRunModel
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

    @strawberry.field
    async def versions(self, info: strawberry.Info[Context, None]) -> list[PromptVersion]:
        """Newest first. Batched so `prompts { versions }` is one query, not
        one per prompt. Same unauthenticated guard as `Agent.config`."""
        if info.context.prompt_versions_loader is None:
            raise AuthenticationError("authentication required")
        models = await info.context.prompt_versions_loader.load(self.id)
        return [PromptVersion.from_model(m) for m in models]


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
    #: Set for a product citation (`search_products`/`get_product`), where
    #: `chunk_id`/`document_id` are `None`. `ON DELETE SET NULL` like the
    #: other two, so a citation of a since-deleted product has all three
    #: `None`.
    product_id: uuid.UUID | None
    #: Untrusted -- the uploaded document's own title (or, for a product
    #: citation, the product's name), never escaped by the server.
    #: Rendering it must go through JSX text interpolation only; see the
    #: same warning on `Citation` in `apps/web/src/lib/sse.ts`.
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
            product_id=model.product_id,
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


# ---------------------------------------------------------------------------
# Phase 7 -- MCP (docs/PHASE-7.md §6): API keys and an agent's MCP surface.
# ---------------------------------------------------------------------------


@strawberry.type
class ApiKey:
    """One credential row for the dashboard's key list (§6). Deliberately has
    no field for the plaintext secret at all -- `CreatedApiKey.token` is a
    sibling field on `createApiKey`'s own response type, never on this one,
    so there is no field for a client to even try asking `apiKeys` for."""

    id: uuid.UUID
    name: str
    key_prefix: str
    agent_id: uuid.UUID
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    _created_by: strawberry.Private[uuid.UUID | None]

    @classmethod
    def from_model(cls, model: ApiKeyModel) -> "ApiKey":
        return cls(
            id=model.id,
            name=model.name,
            key_prefix=model.key_prefix,
            agent_id=model.agent_id,
            created_at=model.created_at,
            last_used_at=model.last_used_at,
            revoked_at=model.revoked_at,
            _created_by=model.created_by,
        )

    @strawberry.field
    async def created_by_name(self, info: strawberry.Info[Context, None]) -> str | None:
        """The creator's full name, batched through a dataloader -- same
        unauthenticated guard as `Agent.config`. `None` without a query at
        all when the creator has been deleted (`created_by IS NULL`, `ON
        DELETE SET NULL`), and `None` after the loader's own lookup when the
        creator still exists but is no longer a member of this organization
        -- that lookup is scoped to THIS organization's `memberships`, never
        an unscoped `users` read, because `users` is a global table with no
        `organization_id` of its own."""
        if self._created_by is None:
            return None
        if info.context.api_key_creator_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.api_key_creator_loader.load(self._created_by)


@strawberry.type
class CreatedApiKey:
    """`createApiKey`'s response -- the only place a plaintext token is ever
    returned (docs/PHASE-7.md §3/§6). It is generated, hashed and stored,
    and handed back exactly once; no other query or mutation response ever
    carries it."""

    api_key: ApiKey
    token: str

    @classmethod
    def from_service(cls, created: CreatedApiKeyResult) -> "CreatedApiKey":
        return cls(api_key=ApiKey.from_model(created.api_key), token=created.token)


@strawberry.type
class McpInfo:
    """An agent's MCP surface (docs/PHASE-7.md §5/§6): its granted tool
    names intersected with `MCP_EXPOSED_TOOL_NAMES` -- exactly what an MCP
    client authenticated as one of this agent's keys would see from
    `tools/list` (`app/mcp/server.py`'s own `_exposed`). `create_lead` can
    never appear here even if granted to the agent: it is not in
    `MCP_EXPOSED_TOOL_NAMES` at all, because an MCP call has no conversation
    for a lead to belong to."""

    exposed_tool_names: list[str]


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
    #: On a FAILED import, the whole-file failure (bad JSON, a missing
    #: required column), set instead of any per-row error. On a COMPLETED
    #: import, a non-fatal warning about the import as a whole -- today only
    #: that the rows landed but could not all be embedded.
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


# ---------------------------------------------------------------------------
# Phase 6 -- evaluations (docs/PHASE-6.md). Starting a run is REST
# (`POST /api/v1/evaluations/runs`), not a mutation -- see the comment above
# `Mutation` in resolvers.py and docs/PHASE-6.md §7 for why. Everything else
# (dataset/case CRUD, listing runs and results, cancelling) is here.
# ---------------------------------------------------------------------------


@strawberry.enum
class EvaluationRunStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _json_text(value: Any) -> str:
    """Free-form jsonb (a score's `detail`, a tool call's `arguments`)
    flattened to a JSON string, the same idiom `_attribute_value` above uses
    for product attributes -- the dashboard parses it client-side rather than
    the server committing to a typed shape for something this open-ended."""
    return json.dumps(value, ensure_ascii=False)


@strawberry.type
class EvaluationDataset:
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, model: EvalDatasetModel) -> "EvaluationDataset":
        return cls(
            id=model.id,
            name=model.name,
            description=model.description,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @strawberry.field
    async def case_count(self, info: strawberry.Info[Context, None]) -> int:
        # Same reasoning as `Document.chunk_count`: an unauthenticated
        # request builds a Context with no loader, and that must fail as a
        # clean `unauthenticated` error from the `evaluationDatasets`/
        # `evaluationDataset` resolver itself, not an AttributeError here.
        if info.context.eval_case_count_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.eval_case_count_loader.load(self.id)

    @strawberry.field
    async def latest_run(self, info: strawberry.Info[Context, None]) -> "EvaluationRun | None":
        """The dashboard's dataset list shows the most recent run's status
        without a second round trip per row -- batched for the same reason
        `case_count` is."""
        if info.context.eval_latest_run_loader is None:
            raise AuthenticationError("authentication required")
        model = await info.context.eval_latest_run_loader.load(self.id)
        return EvaluationRun.from_model(model) if model else None


@strawberry.type
class EvaluationCase:
    id: uuid.UUID
    question: str
    reference_answer: str | None
    required_phrases: list[str]
    expected_tool_names: list[str]
    expected_document_ids: list[uuid.UUID]
    expected_product_ids: list[uuid.UUID]
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, model: EvalCaseModel) -> "EvaluationCase":
        return cls(
            id=model.id,
            question=model.question,
            reference_answer=model.reference_answer,
            required_phrases=list(model.required_phrases),
            expected_tool_names=list(model.expected_tool_names),
            expected_document_ids=list(model.expected_document_ids),
            expected_product_ids=list(model.expected_product_ids),
            tags=list(model.tags),
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


# The fixed order docs/PHASE-6.md §5's summary and this task's brief both
# name: judge first (it is the one a human reads a rationale for), then the
# three deterministic recall/selection scorers. Not-applicable scorers are
# simply absent from a given result or run, never emitted as a null entry.
_SCORE_ORDER: tuple[str, ...] = (
    "judge",
    "required_phrases",
    "tool_selection",
    "document_recall",
    "product_recall",
)


@strawberry.type
class EvaluationScore:
    name: str
    score: float | None
    passed: bool
    status: str
    #: `score_to_json`'s `detail` -- shape differs per scorer (missing
    #: phrases, missing tool names, a judge's rationale), so it crosses the
    #: wire as JSON text rather than a scorer-specific type.
    detail: str


@strawberry.type
class EvaluationToolCall:
    name: str
    #: The tool call's arguments, as the model produced them -- JSON text,
    #: same reasoning as `EvaluationScore.detail`.
    arguments: str
    is_error: bool
    #: `app.rag.retrieve.excerpt`'s bounded excerpt of the tool's full
    #: result, not the result itself -- the eval result row never stores the
    #: full content (docs/PHASE-6.md §2).
    excerpt: str


@strawberry.type
class EvaluationResult:
    id: uuid.UUID
    case_id: uuid.UUID | None
    question: str
    answer: str | None
    error: str | None
    passed: bool
    cited_document_ids: list[uuid.UUID]
    cited_product_ids: list[uuid.UUID]
    prompt_version_id: uuid.UUID | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    #: A string, not a float, for the same reason `Product.price` and
    #: `Message.cost_usd` are: `Numeric(12, 6)` is exact.
    cost_usd: str | None
    created_at: datetime
    _scores: strawberry.Private[dict[str, Any]]
    _tool_calls: strawberry.Private[list[dict[str, Any]]]

    @classmethod
    def from_model(cls, model: EvalResultModel) -> "EvaluationResult":
        return cls(
            id=model.id,
            case_id=model.case_id,
            question=model.question,
            answer=model.answer,
            error=model.error,
            passed=model.passed,
            cited_document_ids=list(model.cited_document_ids),
            cited_product_ids=list(model.cited_product_ids),
            prompt_version_id=model.prompt_version_id,
            latency_ms=model.latency_ms,
            input_tokens=model.input_tokens,
            output_tokens=model.output_tokens,
            cost_usd=None if model.cost_usd is None else str(model.cost_usd),
            created_at=model.created_at,
            _scores=model.scores,
            _tool_calls=model.tool_calls,
        )

    @strawberry.field
    def scores(self) -> list[EvaluationScore]:
        """Ordered per `_SCORE_ORDER`, not `dict` insertion order -- the
        runner writes whichever scorers applied in whatever order it
        computed them, and the brief pins a fixed, human-meaningful order
        (judge first) for the dashboard to render."""
        return [
            EvaluationScore(
                name=key,
                score=self._scores[key].get("score"),
                passed=self._scores[key]["passed"],
                status=self._scores[key]["status"],
                detail=_json_text(self._scores[key].get("detail", {})),
            )
            for key in _SCORE_ORDER
            if key in self._scores
        ]

    @strawberry.field
    def tool_calls(self) -> list[EvaluationToolCall]:
        return [
            EvaluationToolCall(
                name=call["name"],
                arguments=_json_text(call.get("arguments", {})),
                is_error=call["is_error"],
                excerpt=call["excerpt"],
            )
            for call in self._tool_calls
        ]


@strawberry.type
class EvaluationScorerSummary:
    name: str
    mean: float | None
    passed: int
    applicable: int


@strawberry.type
class EvaluationRunSummary:
    passed: int
    failed: int
    errored: int
    pass_rate: float | None
    #: A string, not a float -- the exact `Decimal` sum `build_summary`
    #: writes, same reasoning as `EvaluationResult.cost_usd`. `None` as soon
    #: as any priced component was unpriced.
    cost_usd: str | None
    mean_latency_ms: int | None
    #: In `_SCORE_ORDER`, only the scorers that had at least one applicable
    #: case in this run -- the dashboard's per-scorer means (Task 6).
    scorers: list[EvaluationScorerSummary]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "EvaluationRunSummary":
        scorers_raw = raw.get("scorers") or {}
        return cls(
            passed=raw["passed"],
            failed=raw["failed"],
            errored=raw["errored"],
            pass_rate=raw.get("pass_rate"),
            cost_usd=raw.get("cost_usd"),
            mean_latency_ms=raw.get("mean_latency_ms"),
            scorers=[
                EvaluationScorerSummary(
                    name=key,
                    mean=scorers_raw[key].get("mean"),
                    passed=scorers_raw[key]["passed"],
                    applicable=scorers_raw[key]["applicable"],
                )
                for key in _SCORE_ORDER
                if key in scorers_raw
            ],
        )


@strawberry.type
class EvaluationRun:
    id: uuid.UUID
    dataset_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version_id: uuid.UUID | None
    provider: str
    model: str
    judge_provider: str | None
    judge_model: str | None
    status: EvaluationRunStatus
    case_count: int
    completed_count: int
    error: str | None
    triggered_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    _summary: strawberry.Private[dict[str, Any]]

    @classmethod
    def from_model(cls, model: EvalRunModel) -> "EvaluationRun":
        return cls(
            id=model.id,
            dataset_id=model.dataset_id,
            agent_id=model.agent_id,
            prompt_version_id=model.prompt_version_id,
            provider=model.provider,
            model=model.model,
            judge_provider=model.judge_provider,
            judge_model=model.judge_model,
            status=EvaluationRunStatus(model.status.value),
            case_count=model.case_count,
            completed_count=model.completed_count,
            error=model.error,
            triggered_by=model.triggered_by,
            created_at=model.created_at,
            started_at=model.started_at,
            finished_at=model.finished_at,
            _summary=model.summary,
        )

    @strawberry.field
    def summary(self) -> EvaluationRunSummary | None:
        """`None` until the run completes -- `EvalRun.summary` defaults to
        `{}` (Ruling 1: a typed object, not raw JSON, so the dashboard never
        parses jsonb client-side)."""
        if not self._summary:
            return None
        return EvaluationRunSummary.from_json(self._summary)

    @strawberry.field
    async def agent_name(self, info: strawberry.Info[Context, None]) -> str | None:
        """Batched across a page of runs -- see `Context._load_eval_agent_names`.
        `None` only in principle: `eval_runs.agent_id` is `ON DELETE CASCADE`
        from `agents`, so a run always has a live agent, but a loader keyed
        by id has no way to assume that stays true forever (same reasoning as
        `Lead.conversation`)."""
        if info.context.eval_agent_name_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.eval_agent_name_loader.load(self.agent_id)

    @strawberry.field
    async def prompt_version(self, info: strawberry.Info[Context, None]) -> int | None:
        """The pinned `PromptVersion.version` number, not its id -- the
        dashboard wants "v3", not a UUID. `None` when no version was pinned
        at all (the agent had no prompt) or, in principle, when the pinned
        version has since been deleted (`ON DELETE SET NULL`)."""
        if self.prompt_version_id is None:
            return None
        if info.context.eval_prompt_version_loader is None:
            raise AuthenticationError("authentication required")
        return await info.context.eval_prompt_version_loader.load(self.prompt_version_id)

    @strawberry.field
    async def results(self, info: strawberry.Info[Context, None]) -> list[EvaluationResult]:
        """Resolved only when asked for -- `evaluationRuns` (the list) never
        touches `eval_results` at all, since this field is not selected
        there; `evaluationRun(id)` (the detail page) is the one place a
        client asks for it. Still batched through a loader, matching every
        other per-parent collection in this file (`Message.citations`,
        `Conversation.messages`), so a query that *did* ask for it on a list
        would cost one query rather than one per run."""
        if info.context.eval_results_loader is None:
            raise AuthenticationError("authentication required")
        rows = await info.context.eval_results_loader.load(self.id)
        return [EvaluationResult.from_model(row) for row in rows]


@strawberry.input
class CreateEvaluationDatasetInput:
    name: str
    description: str | None = None


@strawberry.input
class UpdateEvaluationDatasetInput:
    name: str | None = None
    description: str | None = None


@strawberry.input
class EvaluationCaseInput:
    """The full write shape for both `createEvaluationCase` and
    `updateEvaluationCase` -- `updateEvaluationCase` is a full replace, not a
    patch, matching `EvaluationService.update_case`/`CaseInput`."""

    question: str
    reference_answer: str | None = None
    required_phrases: list[str] = strawberry.field(default_factory=list)
    expected_tool_names: list[str] = strawberry.field(default_factory=list)
    expected_document_ids: list[uuid.UUID] = strawberry.field(default_factory=list)
    expected_product_ids: list[uuid.UUID] = strawberry.field(default_factory=list)
    tags: list[str] = strawberry.field(default_factory=list)
