import uuid
from typing import Any

import strawberry
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.agents import schemas as agent_schemas
from app.agents.service import AgentService
from app.core.errors import AuthenticationError, format_validation_errors
from app.core.errors import ValidationError as AppValidationError
from app.db.models import Membership, Organization
from app.db.models import User as UserModel
from app.graphql import types as gql
from app.graphql.context import Context
from app.llm.catalog import models_for
from app.prompts import schemas as prompt_schemas
from app.prompts.service import PromptService

Info = strawberry.Info[Context, None]


def _build[ModelT: BaseModel](schema_cls: type[ModelT], **fields: Any) -> ModelT:
    """Construct a pydantic input model from GraphQL input fields.

    Named `schema_cls`/`fields` rather than `model`/`kwargs`: several of the
    schemas being built (e.g. `CreateAgentInput`) have their own field
    literally named `model` (the LLM model id), which would collide with a
    parameter of that name.

    `agent_schemas`/`prompt_schemas` models carry constraints (e.g.
    `temperature: ge=0.0, le=2.0`) that the GraphQL input types themselves do
    not enforce. `pydantic.ValidationError` is a different class from
    `app.core.errors.ValidationError`, so without this translation it would
    fall through the schema's `AppErrorExtension` unrecognised - reaching the
    client as a raw pydantic message (field paths, constraint internals, an
    errors.pydantic.dev URL) with no `extensions.code`. Route it through the
    app's own `ValidationError` instead, so it renders as `invalid_input`
    with an actionable message. The rendering itself lives in
    `app.core.errors.format_validation_errors`, shared with the REST
    `RequestValidationError` handler so both surfaces say the same thing -
    and so neither ever echoes the rejected `input` value back.
    """
    try:
        return schema_cls(**fields)
    except PydanticValidationError as exc:
        raise AppValidationError(format_validation_errors(exc.errors())) from exc


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
                # REST /me rejects a deactivated account; without this the
                # two surfaces disagree about who is still allowed in.
                UserModel.is_active.is_(True),
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
    async def organization(self, info: Info) -> gql.Organization:
        """The caller's own organization. Takes no id argument on purpose: in
        Phase 1 a token carries exactly one `org` claim and a caller belongs
        to exactly one organization, so an id parameter would be a
        tenant-scoping decision handed to the client — the one thing the
        tenancy model never does."""
        _require_tenant(info)
        tenant = info.context.tenant
        session = info.context.session
        assert tenant is not None
        assert session is not None
        result = await session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
        organization = result.scalar_one_or_none()
        if organization is None:
            raise AuthenticationError("organization no longer exists")
        return gql.Organization.from_model(organization)

    @strawberry.field
    async def agents(self, info: Info) -> list[gql.Agent]:
        return [gql.Agent.from_model(a) for a in await _agents(info).list_agents()]

    @strawberry.field
    async def agent(self, info: Info, id: uuid.UUID) -> gql.Agent:
        return gql.Agent.from_model(await _agents(info).get_agent(id))

    @strawberry.field
    async def provider_models(self, info: Info, provider: str) -> list[gql.ModelOption]:
        """The models the agent form may offer for `provider`.

        Authenticated like every other query even though it reads no tenant
        data: it is reachable only from the dashboard, and an unauthenticated
        endpoint that makes this server issue an outbound request on demand is
        a free amplifier.
        """
        _require_tenant(info)
        return [
            gql.ModelOption(id=option.id, label=option.label, context_length=option.context_length)
            for option in await models_for(provider)
        ]

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
            _build(
                agent_schemas.CreateAgentInput,
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
        payload = _build(
            agent_schemas.UpdateAgentInput,
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
        payload = _build(
            agent_schemas.UpdateAgentConfigInput,
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
            _build(
                prompt_schemas.CreatePromptInput,
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
            _build(
                prompt_schemas.CreateVersionInput,
                system_prompt=input.system_prompt,
                notes=input.notes,
            ),
        )
        return gql.PromptVersion.from_model(version)

    @strawberry.mutation
    async def activate_prompt_version(self, info: Info, version_id: uuid.UUID) -> gql.PromptVersion:
        version = await _prompts(info).activate_version(version_id)
        return gql.PromptVersion.from_model(version)
