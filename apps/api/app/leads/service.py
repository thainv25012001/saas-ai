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
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == self.tenant.organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("conversation not found")

        lead = Lead(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            name=data.name,
            email=data.email,
            phone=data.phone,
            interest=data.interest,
        )
        self.session.add(lead)
        await self.session.flush()
        return lead
