import uuid

import strawberry
from sqlalchemy import select

from app.agents import schemas as agent_schemas
from app.agents.service import AgentService
from app.core.errors import AuthenticationError
from app.db.models import Membership, Organization
from app.db.models import User as UserModel
from app.graphql import types as gql
from app.graphql.context import Context
from app.prompts import schemas as prompt_schemas
from app.prompts.service import PromptService

Info = strawberry.Info[Context, None]


def _require_tenant(info: Info) -> None:
    """Every resolver's first line. Authentication is checked here rather than
    in the context builder so the failure is rendered as a GraphQL error with
    an `extensions.code`, matching the REST envelope."""
    if info.context.tenant is None:
        raise AuthenticationError("authentication required")


def _agents(info: Info) -> AgentService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return AgentService(info.context.session, info.context.tenant)


def _prompts(info: Info) -> PromptService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return PromptService(info.context.session, info.context.tenant)


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info: Info) -> gql.Me:
        _require_tenant(info)
        tenant = info.context.tenant
        session = info.context.session
        assert tenant is not None
        assert session is not None
        result = await session.execute(
            select(UserModel, Organization, Membership)
            .join(Membership, Membership.user_id == UserModel.id)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                UserModel.id == tenant.user_id,
                Membership.organization_id == tenant.organization_id,
            )
        )
        row = result.first()
        if row is None:
            raise AuthenticationError("account no longer exists")
        user, organization, membership = row
        return gql.Me(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            organization_id=organization.id,
            organization_name=organization.name,
            role=membership.role.value,
        )

    @strawberry.field
    async def agents(self, info: Info) -> list[gql.Agent]:
        return [gql.Agent.from_model(a) for a in await _agents(info).list_agents()]

    @strawberry.field
    async def agent(self, info: Info, id: uuid.UUID) -> gql.Agent:
        return gql.Agent.from_model(await _agents(info).get_agent(id))

    @strawberry.field
    async def prompts(self, info: Info) -> list[gql.Prompt]:
        return [gql.Prompt.from_model(p) for p in await _prompts(info).list_prompts()]

    @strawberry.field
    async def prompt(self, info: Info, id: uuid.UUID) -> gql.Prompt:
        return gql.Prompt.from_model(await _prompts(info).get_prompt(id))


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_agent(self, info: Info, input: gql.CreateAgentInput) -> gql.Agent:
        agent = await _agents(info).create_agent(
            agent_schemas.CreateAgentInput(
                name=input.name,
                provider=input.provider,
                model=input.model,
                temperature=input.temperature,
                max_tokens=input.max_tokens,
            )
        )
        return gql.Agent.from_model(agent)

    @strawberry.mutation
    async def update_agent(
        self, info: Info, id: uuid.UUID, input: gql.UpdateAgentInput
    ) -> gql.Agent:
        payload = agent_schemas.UpdateAgentInput(
            name=input.name,
            status=input.status.value if input.status else None,
            provider=input.provider,
            model=input.model,
            temperature=input.temperature,
            max_tokens=input.max_tokens,
        )
        return gql.Agent.from_model(await _agents(info).update_agent(id, payload))

    @strawberry.mutation
    async def update_agent_config(
        self,
        info: Info,
        agent_id: uuid.UUID,
        input: gql.UpdateAgentConfigInput,
    ) -> gql.AgentConfig:
        payload = agent_schemas.UpdateAgentConfigInput(
            persona=input.persona,
            tone=input.tone,
            language=input.language,
            greeting=input.greeting,
            fallback_message=input.fallback_message,
            enabled_tool_names=input.enabled_tool_names,
            retrieval_top_k=input.retrieval_top_k,
            retrieval_min_score=input.retrieval_min_score,
            max_agent_steps=input.max_agent_steps,
        )
        config = await _agents(info).update_config(agent_id, payload)
        return gql.AgentConfig.from_model(config)

    @strawberry.mutation
    async def delete_agent(self, info: Info, id: uuid.UUID) -> bool:
        await _agents(info).delete_agent(id)
        return True

    @strawberry.mutation
    async def create_prompt(self, info: Info, input: gql.CreatePromptInput) -> gql.Prompt:
        prompt = await _prompts(info).create_prompt(
            prompt_schemas.CreatePromptInput(
                name=input.name,
                key=input.key,
                description=input.description,
                system_prompt=input.system_prompt,
            )
        )
        return gql.Prompt.from_model(prompt)

    @strawberry.mutation
    async def create_prompt_version(
        self,
        info: Info,
        prompt_id: uuid.UUID,
        input: gql.CreatePromptVersionInput,
    ) -> gql.PromptVersion:
        version = await _prompts(info).create_version(
            prompt_id,
            prompt_schemas.CreateVersionInput(system_prompt=input.system_prompt, notes=input.notes),
        )
        return gql.PromptVersion.from_model(version)

    @strawberry.mutation
    async def activate_prompt_version(self, info: Info, version_id: uuid.UUID) -> gql.PromptVersion:
        version = await _prompts(info).activate_version(version_id)
        return gql.PromptVersion.from_model(version)
