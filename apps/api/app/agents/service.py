import secrets
import uuid
from typing import Any

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import (
    CreateAgentInput,
    UpdateAgentConfigInput,
    UpdateAgentInput,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Agent, AgentConfig, AgentStatus

_DEFAULT_FALLBACK = (
    "I don't have that information. Would you like me to connect you with someone who does?"
)


class AgentService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def list_agents(self) -> list[Agent]:
        result = await self.session.execute(
            select(Agent)
            .where(Agent.organization_id == self.tenant.organization_id)
            .order_by(Agent.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_agent(self, agent_id: uuid.UUID) -> Agent:
        result = await self.session.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.organization_id == self.tenant.organization_id,
            )
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            raise NotFoundError("agent not found")
        return agent

    async def get_config(self, agent_id: uuid.UUID) -> AgentConfig:
        await self.get_agent(agent_id)  # 404s for other tenants before touching config
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.agent_id == agent_id)
        )
        config = result.scalar_one_or_none()
        if config is None:
            raise NotFoundError("agent config not found")
        return config

    async def create_agent(self, data: CreateAgentInput) -> Agent:
        agent = Agent(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            name=data.name,
            slug=slugify(data.name)[:120] or "agent",
            status=AgentStatus.DRAFT,
            provider=data.provider,
            model=data.model,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
            public_key=f"pk_{secrets.token_urlsafe(24)}",
        )
        config = AgentConfig(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent.id,
            fallback_message=_DEFAULT_FALLBACK,
            enabled_tool_names=[],
        )
        self.session.add_all([agent, config])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"an agent named '{data.name}' already exists") from exc
        return agent

    async def update_agent(self, agent_id: uuid.UUID, data: UpdateAgentInput) -> Agent:
        agent = await self.get_agent(agent_id)
        updates: dict[str, Any] = data.model_dump(exclude_unset=True, exclude_none=True)

        if "name" in updates:
            agent.slug = slugify(updates["name"])[:120] or "agent"
        if "status" in updates:
            agent.status = AgentStatus(updates.pop("status"))
        for field, value in updates.items():
            setattr(agent, field, value)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("an agent with that name already exists") from exc
        return agent

    async def update_config(self, agent_id: uuid.UUID, data: UpdateAgentConfigInput) -> AgentConfig:
        config = await self.get_config(agent_id)
        updates: dict[str, Any] = data.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in updates.items():
            setattr(config, field, value)
        await self.session.flush()
        return config

    async def delete_agent(self, agent_id: uuid.UUID) -> None:
        agent = await self.get_agent(agent_id)
        await self.session.delete(agent)
        await self.session.flush()
