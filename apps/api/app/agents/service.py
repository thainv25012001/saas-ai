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
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.builtin_tools import DEFAULT_ENABLED_TOOL_NAMES
from app.db.models import Agent, AgentConfig, AgentStatus, AgentToolLink, Tool, ToolType

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
        # `provider` and `model` are taken exactly as given: `CreateAgentInput`
        # makes both required and validates the provider against
        # `registry.KNOWN_PROVIDERS`, so there is nothing left to resolve here.
        #
        # This used to read `data.provider or get_settings().default_llm_provider`
        # with a matching `DEFAULT_MODELS` lookup for the model. The dashboard
        # sent neither field, so that fallback ran for every agent a real user
        # created and silently put them all on `fake`. The choice belongs to
        # whoever is creating the agent; `DEFAULT_LLM_PROVIDER` now has exactly
        # one consumer, the dev seed in `app.db.seed`, which asks for it by name.
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
        )
        # Link the default builtins in the SAME flush as `agent`/`config`,
        # not a later one: a caller that reads `agent_tools` back before any
        # later commit (Task 7b's own test does exactly this) must already
        # see it, and a partial failure must not leave `agent`/`config`
        # committed with no tools at all. `agent.id`/`link.agent_id` are
        # both client-generated (`uuid7()`), so this needs no round trip to
        # the database to learn `agent.id` before building the links.
        # See `app.db.builtin_tools` for which names are default-enabled
        # and why, and `AgentToolLink.organization_id` for why it repeats
        # `self.tenant.organization_id` rather than joining through `agent`.
        default_tool_ids = await self._default_builtin_tool_ids()
        links = [
            AgentToolLink(
                agent_id=agent.id,
                tool_id=tool_id,
                organization_id=self.tenant.organization_id,
            )
            for tool_id in default_tool_ids
        ]
        self.session.add_all([agent, config, *links])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"an agent named '{data.name}' already exists") from exc
        return agent

    async def _default_builtin_tool_ids(self) -> list[uuid.UUID]:
        """The global (`organization_id IS NULL`) builtin `tools` rows a
        brand-new agent is linked to, enabled, by default -- seeded by
        migration `0009_seed_builtin_tools`. Returns an empty list rather
        than raising if that migration has not run (e.g. a stale dev
        database): a new agent with no tools linked is exactly Task 7's
        pre-Task-7b behaviour, not a crash on agent creation.
        """
        result = await self.session.execute(
            select(Tool.id).where(
                Tool.organization_id.is_(None),
                Tool.type == ToolType.BUILTIN,
                Tool.is_enabled.is_(True),
                Tool.name.in_(DEFAULT_ENABLED_TOOL_NAMES),
            )
        )
        return list(result.scalars().all())

    async def update_agent(self, agent_id: uuid.UUID, data: UpdateAgentInput) -> Agent:
        agent = await self.get_agent(agent_id)
        updates: dict[str, Any] = data.model_dump(exclude_unset=True, exclude_none=True)

        if "name" in updates:
            agent.slug = slugify(updates["name"])[:120] or "agent"
        if "status" in updates:
            raw_status = updates.pop("status")
            try:
                agent.status = AgentStatus(raw_status)
            except ValueError as exc:
                # A caller-supplied string, not a token or any other trusted
                # input - name the valid options so the caller can act on it.
                # See app/auth/dependencies.py for the same ValueError ->
                # domain-error conversion for MembershipRole; here the value
                # comes from the request, not from a signed token, so
                # ValidationError (422, invalid_input) is the right domain
                # error rather than an auth error.
                valid = ", ".join(status.value for status in AgentStatus)
                raise ValidationError(f"status must be one of: {valid}") from exc
        for field, value in updates.items():
            setattr(agent, field, value)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("an agent with that name already exists") from exc

        # `updated_at` (onupdate=func.now()) is expired by the flush above
        # rather than populated eagerly, because this UPDATE only recomputes
        # it when the row already exists (unlike an INSERT, where `flush()`
        # eagerly returns every server-generated column). A later plain
        # attribute access - e.g. GraphQL's Agent.from_model reading
        # `model.updated_at` - would trigger a lazy reload synchronously,
        # which raises `MissingGreenlet` outside of an active async context.
        # `refresh()` reloads it here, through the session's own async path,
        # while we can still safely await.
        await self.session.refresh(agent)
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
