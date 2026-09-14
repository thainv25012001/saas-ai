import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.schemas import AppendMessageInput, CreateConversationInput, RecordUsageInput
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Agent, Conversation, ConversationMessage, ConversationStatus, UsageEvent


class ConversationService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(self, agent_id: uuid.UUID, data: CreateConversationInput) -> Conversation:
        # Load-bearing, not belt-and-braces: a Postgres FK check bypasses the
        # referencing session's RLS policy, so without this explicit,
        # tenant-scoped SELECT the INSERT below would happily create a
        # conversation whose agent_id FK points at another org's agent. See
        # the longer note on record_usage(), which has the same shape.
        result = await self.session.execute(
            select(Agent.id).where(
                Agent.id == agent_id,
                Agent.organization_id == self.tenant.organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("agent not found")

        conversation = Conversation(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent_id,
            visitor_id=data.visitor_id,
            channel=data.channel,
            status=ConversationStatus.OPEN,
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def get(self, conversation_id: uuid.UUID) -> Conversation:
        result = await self.session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == self.tenant.organization_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            # Cross-tenant lookups must fail the same way a nonexistent id
            # does. Anything that distinguishes "not yours" from "does not
            # exist" (e.g. PermissionDeniedError) confirms the row exists.
            raise NotFoundError("conversation not found")
        return conversation

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[Conversation]:
        result = await self.session.execute(
            select(Conversation)
            .where(
                Conversation.agent_id == agent_id,
                Conversation.organization_id == self.tenant.organization_id,
            )
            .order_by(Conversation.created_at.desc())
        )
        return list(result.scalars().all())

    async def next_seq(self, conversation_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.coalesce(func.max(ConversationMessage.seq), 0) + 1).where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.organization_id == self.tenant.organization_id,
            )
        )
        return result.scalar_one()

    async def history(
        self, conversation_id: uuid.UUID, limit: int | None = None
    ) -> list[ConversationMessage]:
        """The most recent `limit` messages, in ascending `seq` order.

        `ORDER BY seq LIMIT N` would return the *oldest* N messages instead —
        correct-looking in any test with fewer messages than the limit, but
        silently feeding a long-running conversation stale context. Ordering
        descending, limiting, then reversing in Python is what actually
        implements "most recent N, oldest first".
        """
        await self.get(conversation_id)  # 404s for other tenants first

        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.organization_id == self.tenant.organization_id,
            )
            .order_by(ConversationMessage.seq.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        messages = list(result.scalars().all())
        messages.reverse()
        return messages

    async def append_message(
        self, conversation_id: uuid.UUID, data: AppendMessageInput
    ) -> ConversationMessage:
        conversation = await self.get(conversation_id)  # enforces tenant ownership

        # Allocated inside the caller's transaction; the unique
        # (conversation_id, seq) constraint is the backstop against two
        # concurrent appends computing the same next value.
        seq = await self.next_seq(conversation_id)
        message = ConversationMessage(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            conversation_id=conversation_id,
            seq=seq,
            role=data.role,
            content=data.content,
            content_blocks=data.content_blocks,
            prompt_version_id=data.prompt_version_id,
            provider=data.provider,
            model=data.model,
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            cost_usd=data.cost_usd,
            latency_ms=data.latency_ms,
            finish_reason=data.finish_reason,
            error=data.error,
        )
        self.session.add(message)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("a message with that sequence number already exists") from exc

        conversation.last_message_at = message.created_at
        await self.session.flush()
        return message

    async def record_usage(self, data: RecordUsageInput) -> UsageEvent:
        # Postgres FK integrity checks run with elevated privileges and are
        # not subject to the referencing session's RLS `USING` policy — a
        # SELECT under this tenant's RLS sees zero rows for another org's
        # agent/conversation, but an INSERT whose FK merely points at that
        # row still succeeds. RLS therefore protects reads of this table
        # (nobody outside the org can see the row afterwards) but does
        # nothing to stop it from being *written* with a dangling
        # cross-tenant reference. These explicit, tenant-scoped existence
        # checks are what actually close that gap, mirroring `create()`'s
        # agent check and `append_message()`'s `self.get()` call.
        if data.agent_id is not None:
            result = await self.session.execute(
                select(Agent.id).where(
                    Agent.id == data.agent_id,
                    Agent.organization_id == self.tenant.organization_id,
                )
            )
            if result.scalar_one_or_none() is None:
                raise NotFoundError("agent not found")
        if data.conversation_id is not None:
            await self.get(data.conversation_id)

        event = UsageEvent(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=data.agent_id,
            conversation_id=data.conversation_id,
            kind=data.kind,
            provider=data.provider,
            model=data.model,
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            cost_usd=data.cost_usd,
        )
        self.session.add(event)
        await self.session.flush()
        return event
