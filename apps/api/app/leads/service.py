import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Conversation, Lead
from app.leads.schemas import CreateLeadInput


class LeadService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(
        self, agent_id: uuid.UUID, conversation_id: uuid.UUID, data: CreateLeadInput
    ) -> Lead:
        """Insert one lead, scoped to `self.tenant`.

        Load-bearing, not belt-and-braces: a Postgres FK check bypasses the
        referencing session's RLS policy (`ConversationService.create`'s
        `agent_id` check and `record_usage`'s docstring establish this same
        fact for this codebase). Without this explicit, tenant-scoped SELECT
        first, an INSERT whose `conversation_id` names another org's
        conversation would still succeed -- the FK is satisfied because the
        row exists, just not in this tenant -- and RLS would only stop that
        org from later *reading* the lead back, not stop it being written
        against their conversation in the first place. `agent_id` is not
        re-checked here the same way: unlike `conversation_id` (an
        argument `ConversationService.create` takes from an external
        caller), `ctx.agent_id` reaches `LeadService.create` only via
        `ToolContext`, which `app/tools/base.py` documents as always
        server-resolved from the conversation's own agent -- so a
        `conversation_id` that passes this check already implies a
        consistent `agent_id`.
        """
        result = await self.session.execute(
            select(Conversation.id, Conversation.channel).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == self.tenant.organization_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            raise NotFoundError("conversation not found")
        _conversation_id, channel = row

        lead = Lead(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            name=data.name,
            email=data.email,
            phone=data.phone,
            interest=data.interest,
            source=channel.value,
        )
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def list_for_agent(
        self, agent_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> list[Lead]:
        """Most recently captured first -- the same ordering `conversations`
        uses for the same reason: whoever opens the dashboard's Leads page
        wants to see what just came in, not the oldest row first.

        Scoped by `organization_id`, matching `ConversationService.
        list_for_agent`'s own explicit predicate rather than relying on RLS
        alone (Layer 1 of `docs/ARCHITECTURE.md` §2.3's two-layer model). An
        `agent_id` belonging to another organization's agent -- or one that
        does not exist at all -- returns an empty list rather than a 404: the
        two are indistinguishable from outside the tenant, and the caller
        (`Query.leads`) leans on that the same way `conversations` does.
        """
        result = await self.session.execute(
            select(Lead)
            .where(
                Lead.agent_id == agent_id,
                Lead.organization_id == self.tenant.organization_id,
            )
            .order_by(Lead.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
