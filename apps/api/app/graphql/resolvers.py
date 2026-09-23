import uuid
from typing import Any

import strawberry
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.agents import schemas as agent_schemas
from app.agents.service import AgentService
from app.conversations.service import ConversationService
from app.core.errors import AuthenticationError, NotFoundError, format_validation_errors
from app.core.errors import ValidationError as AppValidationError
from app.db.models import ConversationChannel as ConversationChannelModel
from app.db.models import DocumentStatus as DocumentStatusModel
from app.db.models import Membership, Organization
from app.db.models import ProductAvailability as ProductAvailabilityModel
from app.db.models import User as UserModel
from app.documents.service import DocumentService
from app.graphql import types as gql
from app.graphql.context import Context
from app.leads.service import LeadService
from app.llm.catalog import models_for
from app.llm.registry import KNOWN_PROVIDERS, provider_is_configured
from app.products.importer import ProductImportService
from app.products.service import ProductService
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


def _documents(info: Info) -> DocumentService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return DocumentService(info.context.session, info.context.tenant)


def _conversations(info: Info) -> ConversationService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return ConversationService(info.context.session, info.context.tenant)


def _leads(info: Info) -> LeadService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return LeadService(info.context.session, info.context.tenant)


def _products(info: Info) -> ProductService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return ProductService(info.context.session, info.context.tenant)


def _product_imports(info: Info) -> ProductImportService:
    _require_tenant(info)
    assert info.context.tenant is not None
    assert info.context.session is not None
    return ProductImportService(info.context.session, info.context.tenant)


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
    async def configured_providers(self, info: Info) -> list[gql.ProviderInfo]:
        """Every provider the server knows, and whether its API key is set.

        The dashboard builds its provider dropdown from this rather than from a
        hardcoded list of its own: a provider added to `KNOWN_PROVIDERS` shows
        up in the UI without a second edit, and one with no key is greyed out
        instead of producing an agent that fails at chat time.

        Authenticated like `provider_models` above -- it reads no tenant data,
        but which integrations an install has configured is not public.
        """
        _require_tenant(info)
        return [
            gql.ProviderInfo(id=name, configured=provider_is_configured(name))
            for name in KNOWN_PROVIDERS
        ]

    @strawberry.field
    async def prompts(self, info: Info) -> list[gql.Prompt]:
        return [gql.Prompt.from_model(p) for p in await _prompts(info).list_prompts()]

    @strawberry.field
    async def prompt(self, info: Info, id: uuid.UUID) -> gql.Prompt:
        return gql.Prompt.from_model(await _prompts(info).get_prompt(id))

    @strawberry.field
    async def documents(
        self,
        info: Info,
        status: gql.DocumentStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[gql.Document]:
        model_status = DocumentStatusModel(status.value) if status is not None else None
        rows = await _documents(info).list_documents(
            status=model_status, limit=limit, offset=offset
        )
        return [gql.Document.from_model(row) for row in rows]

    @strawberry.field
    async def conversations(
        self,
        info: Info,
        agent_id: uuid.UUID,
        channel: gql.ConversationChannel | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[gql.Conversation]:
        """An agent's conversations, most recently active first.

        An agent id belonging to another organization returns an empty list
        rather than an error: it is indistinguishable from one that does not
        exist, and saying which would confirm it exists.
        """
        model_channel = ConversationChannelModel(channel.value) if channel is not None else None
        rows = await _conversations(info).list_for_agent(
            agent_id, channel=model_channel, limit=limit, offset=offset
        )
        return [gql.Conversation.from_model(row) for row in rows]

    @strawberry.field
    async def conversation(self, info: Info, id: uuid.UUID) -> gql.Conversation | None:
        """Nullable for the same reason `document(id)` is: for the dashboard
        "not yours" and "does not exist" are both nothing to show. The
        service still raises `NotFoundError` underneath -- that distinction
        is what must never reach a client."""
        service = _conversations(info)
        try:
            return gql.Conversation.from_model(await service.get(id))
        except NotFoundError:
            return None

    @strawberry.field
    async def document(self, info: Info, id: uuid.UUID) -> gql.Document | None:
        """Nullable, unlike `agent(id)` above: a hidden document (deleted,
        or belonging to another org) is represented here as simply absent
        rather than as a GraphQL error -- the dashboard's document viewer
        treats "not found" and "not yours" identically, as nothing to show,
        with no error banner to render for either."""
        try:
            return gql.Document.from_model(await _documents(info).get(id))
        except NotFoundError:
            return None

    @strawberry.field
    async def leads(
        self,
        info: Info,
        agent_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[gql.Lead]:
        """An agent's captured leads, most recent first. Like `conversations`
        above, an `agent_id` belonging to another organization returns an
        empty list rather than an error -- indistinguishable from one that
        does not exist, and an error would confirm it does."""
        rows = await _leads(info).list_for_agent(agent_id, limit=limit, offset=offset)
        return [gql.Lead.from_model(row) for row in rows]

    @strawberry.field
    async def products(
        self,
        info: Info,
        search: str | None = None,
        category: str | None = None,
        availability: gql.ProductAvailability | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[gql.Product]:
        """The dashboard's catalogue listing, newest first. `search` is a
        substring match on name or SKU -- see `ProductService.list_products`
        for why this is not the agent's semantic search."""
        model_availability = (
            ProductAvailabilityModel(availability.value) if availability is not None else None
        )
        rows = await _products(info).list_products(
            search=search,
            category=category,
            availability=model_availability,
            limit=limit,
            offset=offset,
        )
        return [gql.Product.from_model(row) for row in rows]

    @strawberry.field
    async def product_categories(self, info: Info) -> list[str]:
        """The distinct categories in the caller's catalogue, for the
        dashboard's category filter."""
        return await _products(info).list_categories()

    @strawberry.field
    async def product_imports(
        self, info: Info, limit: int = 20, offset: int = 0
    ) -> list[gql.ProductImport]:
        """The caller's catalogue imports, newest first, with counts and
        per-row errors. The upload itself is REST
        (`POST /api/v1/products/import`), exactly like documents."""
        rows = await _product_imports(info).list_imports(limit=limit, offset=offset)
        return [gql.ProductImport.from_model(row) for row in rows]

    @strawberry.field
    async def agent_tools(self, info: Info, agent_id: uuid.UUID) -> list[gql.AgentTool]:
        """The dashboard's Task 8 toggle surface: every builtin this agent
        could call, and whether it currently may. See
        `AgentService.list_tools` for the shadowing rule and what
        `is_enabled` means when the agent has no link to a tool at all.

        An `agent_id` belonging to another organization (or not existing)
        returns an empty list, exactly like `leads` and `conversations`
        above -- whole-branch review, Important 5. `AgentService.list_tools`
        still raises underneath, via its `get_agent` ownership check; that
        distinction is what must not reach a client, and it is the *query*
        convention being reconciled here, not the service's. These two
        queries were added in the same commit, take the same `agentId`, and
        are rendered on adjacent pages, so one returning an empty table
        while the other rendered a red error was the narrow version of the
        codebase-wide split the ledger defers to Phase 5. `setAgentToolEnabled`
        keeps raising: a mutation that silently did nothing would be worse
        than one that says it could not.
        """
        try:
            pairs = await _agents(info).list_tools(agent_id)
        except NotFoundError:
            return []
        return [gql.AgentTool.from_pair(tool, is_enabled) for tool, is_enabled in pairs]


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

    @strawberry.mutation
    async def delete_document(self, info: Info, id: uuid.UUID) -> bool:
        # Unlike the `document` query above, this does not catch
        # NotFoundError -- matching `delete_agent`'s convention rather than
        # `document`'s. Reading a hidden document as "nothing to show" is
        # harmless; a delete silently reporting success for a document that
        # was never touched (wrong id, or another org's) is the kind of
        # false positive a client should not be able to mistake for "it's
        # gone now".
        await _documents(info).delete(id)
        return True

    @strawberry.mutation
    async def set_agent_tool_enabled(
        self,
        info: Info,
        agent_id: uuid.UUID,
        tool_id: uuid.UUID,
        is_enabled: bool,
    ) -> gql.AgentTool:
        """Task 8's whole reason for existing: `create_lead` is seeded and
        reachable in principle but linked to no agent by default (see
        `app.db.builtin_tools`), and until this mutation shipped, turning it
        on required a raw database write. Creates the `agent_tools` link if
        none exists yet -- see `AgentService.set_tool_enabled`."""
        tool, enabled = await _agents(info).set_tool_enabled(agent_id, tool_id, is_enabled)
        return gql.AgentTool.from_pair(tool, enabled)
