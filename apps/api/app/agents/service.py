import secrets
import uuid
from collections.abc import Sequence
from typing import Any

from slugify import slugify
from sqlalchemy import or_, select
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
from app.db.builtin_tools import DEFAULT_ENABLED_TOOL_NAMES, first_row_per_name
from app.db.models import Agent, AgentConfig, AgentStatus, AgentToolLink, Tool, ToolType
from app.prompts.service import PromptService

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

    async def set_prompt(self, agent_id: uuid.UUID, prompt_id: uuid.UUID | None) -> Agent:
        """Link the agent to one of this organization's prompts, or unlink it
        with `None` (the agent then answers on the built-in default).

        The prompt is resolved through `PromptService.get_prompt` before it is
        written, and that lookup is load-bearing: `agents.prompt_id` is a plain
        foreign key, and Postgres checks a foreign key without RLS, so the
        constraint alone would accept another organization's prompt id. A
        foreign id is `NotFoundError`, as an unknown one is."""
        agent = await self.get_agent(agent_id)
        if prompt_id is not None:
            await PromptService(self.session, self.tenant).get_prompt(prompt_id)
        agent.prompt_id = prompt_id
        await self.session.flush()
        # Same `updated_at` reload as `update_agent`.
        await self.session.refresh(agent)
        return agent

    async def agents_by_prompt(
        self, prompt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[Agent]]:
        """Agents linked to each of several prompts, by name, keyed by prompt
        id with every requested id present. The batched form behind
        `Prompt.agents`' dataloader; like `PromptService.versions_by_prompt`
        it does not raise for an unknown or foreign id."""
        by_prompt: dict[uuid.UUID, list[Agent]] = {pid: [] for pid in prompt_ids}
        if not prompt_ids:
            return by_prompt
        result = await self.session.execute(
            select(Agent)
            .where(
                Agent.prompt_id.in_(list(prompt_ids)),
                Agent.organization_id == self.tenant.organization_id,
            )
            .order_by(Agent.name)
        )
        for agent in result.scalars().all():
            assert agent.prompt_id is not None
            by_prompt[agent.prompt_id].append(agent)
        return by_prompt

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

    async def list_tools(self, agent_id: uuid.UUID) -> list[tuple[Tool, bool]]:
        """Every builtin this agent could be linked to, and whether it
        actually is -- for Task 8's dashboard toggle, the surface Phase 4
        shipped with no way to reach short of a raw database write (see
        `app.db.builtin_tools`'s module docstring: `create_lead` is seeded
        and reachable in principle but linked to no agent by default).

        `is_enabled` here means "there is an `agent_tools` row for this
        agent and tool, with `is_enabled = true`" -- exactly the condition
        `ChatService._resolve_enabled_tool_names` requires to ever call it,
        via the `LEFT JOIN` below: a tool with no link at all reports
        `False`, the same as one explicitly disabled, because both are
        equally uncallable.

        Applies the same shadowing rule as `_resolve_enabled_tool_names`
        (an org-scoped `tools` row fully shadows a global builtin sharing
        its name) so this list shows exactly the rows that resolver would
        actually consider -- a management screen that listed a shadowed
        global row alongside its org-scoped replacement would let someone
        "enable" a tool whose name resolves to the other row entirely.
        """
        await self.get_agent(agent_id)  # 404s for another tenant's agent id
        stmt = (
            select(Tool, AgentToolLink.is_enabled)
            .outerjoin(
                AgentToolLink,
                (AgentToolLink.tool_id == Tool.id)
                & (AgentToolLink.agent_id == agent_id)
                # Layer 1 (`docs/ARCHITECTURE.md` §2.3), in the ON clause and
                # not the WHERE: a LEFT JOIN whose tenant predicate sits in
                # WHERE silently becomes an INNER JOIN, dropping every tool
                # the agent has no link to at all -- which is exactly the row
                # this query exists to report as `is_enabled: false`. RLS
                # (layer 2) is not the only thing standing between this join
                # and another organization's link row; see
                # `tests/integration/test_agent_tools_layer_1.py`, which
                # isolates this predicate from RLS on an `app_owner` session.
                & (AgentToolLink.organization_id == self.tenant.organization_id),
            )
            .where(
                Tool.type == ToolType.BUILTIN,
                Tool.is_enabled.is_(True),
                or_(
                    Tool.organization_id == self.tenant.organization_id,
                    Tool.organization_id.is_(None),
                ),
            )
            .order_by(Tool.name, Tool.organization_id.is_(None))
        )
        rows = (await self.session.execute(stmt)).all()

        resolved = first_row_per_name(
            (tool.name, (tool, bool(link_enabled))) for tool, link_enabled in rows
        )
        return list(resolved.values())

    async def set_tool_enabled(
        self, agent_id: uuid.UUID, tool_id: uuid.UUID, is_enabled: bool
    ) -> tuple[Tool, bool]:
        """Create or flip this agent's `agent_tools` link for `tool_id`.

        `create_lead` starts with no link row at all (Task 7b's deliberate
        "off by default"), so "enable" here is genuinely an upsert, not just
        a flag flip on a row that already exists -- unlike `Tool.is_enabled`
        itself, which this never touches: a management screen for one
        agent's links has no business turning a tool off for every agent at
        once.
        """
        await self.get_agent(agent_id)  # 404s for another tenant's agent id

        tool_result = await self.session.execute(
            select(Tool).where(
                Tool.id == tool_id,
                Tool.type == ToolType.BUILTIN,
                or_(
                    Tool.organization_id == self.tenant.organization_id,
                    Tool.organization_id.is_(None),
                ),
            )
        )
        tool = tool_result.scalar_one_or_none()
        if tool is None:
            raise NotFoundError("tool not found")

        link_result = await self.session.execute(
            select(AgentToolLink).where(
                AgentToolLink.agent_id == agent_id,
                AgentToolLink.tool_id == tool_id,
                # Layer 1 (§2.3), the sibling of the predicate `list_tools`
                # above and `ChatService._resolve_enabled_tool_names` both
                # carry. Note what it converts: a row for this (agent, tool)
                # labelled with ANOTHER organization is no longer found, so
                # the upsert below attempts an INSERT and trips `agent_tools`'
                # composite primary key instead of silently flipping a
                # foreign tenant's row. A loud integrity error on a row that
                # should not exist is the right end for that case; a silent
                # cross-tenant write is not.
                AgentToolLink.organization_id == self.tenant.organization_id,
            )
        )
        link = link_result.scalar_one_or_none()
        if link is None:
            link = AgentToolLink(
                agent_id=agent_id,
                tool_id=tool_id,
                organization_id=self.tenant.organization_id,
                is_enabled=is_enabled,
            )
            self.session.add(link)
        else:
            link.is_enabled = is_enabled
        await self.session.flush()
        return tool, is_enabled
