import uuid
from collections.abc import AsyncIterator, Sequence

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext

from app.auth.dependencies import tenant_from_bearer
from app.core.errors import AuthenticationError
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import (
    AgentConfig,
    ConversationMessage,
    DocumentChunk,
    MessageCitation,
    MessageRole,
)


class Context(BaseContext):
    """Per-request state. One tenant-bound session for the whole operation,
    so every resolver in a query shares one transaction and one RLS setting.

    `tenant` and `session` are nullable because an unauthenticated request
    still needs a context object: raising during context construction would
    let FastAPI's exception handler return a bare 401 instead of a properly
    shaped GraphQL error. Resolvers call `_require_tenant` instead.

    Inherits `strawberry.fastapi.BaseContext` because current
    strawberry-graphql refuses a custom context object that is not a
    `BaseContext` subclass (or a plain dict) — see `GraphQLRouter`'s
    context-merging dependency.
    """

    def __init__(self, tenant: TenantContext | None, session: AsyncSession | None) -> None:
        super().__init__()
        self.tenant = tenant
        self.session = session
        self.config_loader: DataLoader[uuid.UUID, AgentConfig | None] | None = (
            DataLoader(load_fn=self._load_configs) if session is not None else None
        )
        self.chunk_count_loader: DataLoader[uuid.UUID, int] | None = (
            DataLoader(load_fn=self._load_chunk_counts) if session is not None else None
        )
        self.citation_loader: DataLoader[uuid.UUID, list[MessageCitation]] | None = (
            DataLoader(load_fn=self._load_citations) if session is not None else None
        )
        self.preview_loader: DataLoader[uuid.UUID, str | None] | None = (
            DataLoader(load_fn=self._load_previews) if session is not None else None
        )

    async def _load_previews(self, conversation_ids: Sequence[uuid.UUID]) -> list[str | None]:
        """The first question asked in each conversation, for the list's
        fallback label.

        A field with a loader rather than something the client reads off
        `messages`: resolved per row, a page of 20 conversations would issue
        20 full-transcript queries just to render 20 labels.

        `DISTINCT ON (conversation_id) ... ORDER BY conversation_id, seq`
        takes the lowest `seq` per conversation in one pass -- the first
        question, not the most recent, because a label says what the
        conversation was about.

        The explicit `organization_id` predicate is redundant with RLS and
        deliberately so; see `_load_configs`.
        """
        assert self.session is not None
        assert self.tenant is not None
        result = await self.session.execute(
            select(ConversationMessage.conversation_id, ConversationMessage.content)
            .where(
                ConversationMessage.conversation_id.in_(list(conversation_ids)),
                ConversationMessage.organization_id == self.tenant.organization_id,
                ConversationMessage.role == MessageRole.USER,
            )
            .distinct(ConversationMessage.conversation_id)
            .order_by(ConversationMessage.conversation_id, ConversationMessage.seq)
        )
        first_question = {row.conversation_id: row.content for row in result}
        # One slot per requested id, in order: DataLoader matches positionally.
        return [first_question.get(conversation_id) for conversation_id in conversation_ids]

    async def _load_citations(
        self, message_ids: Sequence[uuid.UUID]
    ) -> list[list[MessageCitation]]:
        """Batches `conversation { messages { citations } }` into one query.

        Without this, reopening a 40-message transcript issues 40 queries
        against `message_citations` -- the same N+1 `_load_configs` below
        exists to prevent, and far easier to hit here because a transcript is
        fetched whole rather than a row at a time.

        The explicit `organization_id` predicate is redundant with Postgres
        RLS and deliberately so, matching `_load_configs`: Layer 1
        (application-layer tenant filtering) admits no exceptions.
        `MessageCitation` carries its own `organization_id`, so this costs no
        join.
        """
        assert self.session is not None
        assert self.tenant is not None
        result = await self.session.execute(
            select(MessageCitation)
            .where(
                MessageCitation.message_id.in_(list(message_ids)),
                MessageCitation.organization_id == self.tenant.organization_id,
            )
            .order_by(MessageCitation.rank)
        )
        by_message: dict[uuid.UUID, list[MessageCitation]] = {}
        for citation in result.scalars().all():
            by_message.setdefault(citation.message_id, []).append(citation)
        # One entry per requested id, in the order asked for: DataLoader
        # matches results to keys positionally, so a message with no
        # citations must still contribute an (empty) slot.
        return [by_message.get(message_id, []) for message_id in message_ids]

    async def _load_configs(self, agent_ids: Sequence[uuid.UUID]) -> list[AgentConfig | None]:
        """Batches `agents { config { ... } }` into one query instead of one
        per agent. Without this, listing 50 agents issues 51 queries.

        The `organization_id` predicate is redundant with Postgres RLS on
        `agent_configs`, and deliberately so: Layer 1 (application-layer
        tenant filtering) admits no exceptions, and this loader was the only
        tenant-table query in the codebase defended by RLS alone.
        `AgentConfig` carries its own `organization_id`, so this costs no
        join.
        """
        assert self.session is not None
        assert self.tenant is not None
        result = await self.session.execute(
            select(AgentConfig).where(
                AgentConfig.agent_id.in_(list(agent_ids)),
                AgentConfig.organization_id == self.tenant.organization_id,
            )
        )
        by_agent = {config.agent_id: config for config in result.scalars().all()}
        return [by_agent.get(agent_id) for agent_id in agent_ids]

    async def _load_chunk_counts(self, document_ids: Sequence[uuid.UUID]) -> list[int]:
        """Batches `documents { chunkCount }` into one query instead of one
        per document, the same reasoning as `_load_configs` above.

        `organization_id` is scoped explicitly for the same reason as
        `_load_configs`'s own predicate: Layer 1 tenant filtering admits no
        exceptions, RLS or not. A document id with genuinely zero chunks
        (nothing ingested yet, or ingestion failed before writing any) has
        no row to `GROUP BY`, so it is missing from `by_document` entirely
        -- the trailing `.get(document_id, 0)` is what turns that absence
        into `0` rather than `None`.
        """
        assert self.session is not None
        assert self.tenant is not None
        result = await self.session.execute(
            select(DocumentChunk.document_id, func.count())
            .where(
                DocumentChunk.document_id.in_(list(document_ids)),
                DocumentChunk.organization_id == self.tenant.organization_id,
            )
            .group_by(DocumentChunk.document_id)
        )
        by_document = {document_id: count for document_id, count in result.all()}
        return [by_document.get(document_id, 0) for document_id in document_ids]


async def build_context(request: Request) -> AsyncIterator[Context]:
    """A generator dependency: FastAPI holds it open for the whole request, so
    the tenant session and its transaction stay alive while resolvers run.

    Trade-off, documented rather than latent: one transaction spans the whole
    GraphQL operation. For the normal shape of a client request - one query
    or one mutation per operation - this is exactly what we want: `me` and
    `agents` see a consistent snapshot, and a failed mutation rolls back
    everything it touched.

    It does mean a multi-root operation with several top-level mutation
    fields, e.g. `mutation { a: createAgent(...) b: createAgent(...) }`,
    does not get per-field atomicity: if `b` fails after `a` already flushed,
    `data.a` still comes back populated in the response even though `a` was
    never committed (the whole transaction rolls back when the request ends
    without a commit), and any resolver that runs after the failure raises
    SQLAlchemy's `PendingRollbackError` because the session is left in a
    failed-transaction state. Client code that only ever sends single-root
    operations - the normal case - never observes this.
    """
    try:
        tenant = tenant_from_bearer(request)
    except AuthenticationError:
        yield Context(tenant=None, session=None)
        return

    async with tenant_session(tenant) as session:
        yield Context(tenant, session)
