"""Idempotent development seed. Safe to run repeatedly.

Idempotent means more than "the second run is a no-op": each of the four
objects below is fetched-or-created independently, so a run against a
database that already has *some* of them (e.g. a test purge deleted the demo
user but left the organization behind — see tests/conftest.py's
`clean_users`, whose `DELETE FROM users WHERE email LIKE '%@example.com'`
matches DEMO_EMAIL) still converges on exactly one of each row instead of
raising on a unique constraint.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import CreateAgentInput
from app.agents.service import AgentService
from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.security import hash_password
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import (
    Agent,
    AgentStatus,
    Membership,
    MembershipRole,
    Organization,
    Prompt,
    User,
)
from app.db.models.widget import WidgetPosition
from app.llm.registry import DEFAULT_MODELS
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService
from app.widget.schemas import UpdateWidgetSettingsInput
from app.widget.service import WidgetSettingsService

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password-123"
DEMO_ORG_NAME = "Demo Motors"
DEMO_ORG_SLUG = "demo-motors"
DEMO_PROMPT_KEY = "sales_system"
DEMO_AGENT_NAME = "Demo Sales Agent"
DEMO_AGENT_SLUG = "demo-sales-agent"
# The static demo page (`infrastructure/widget-demo`, `make widget-demo`) is
# served on its own port so it is a distinct origin from the dashboard --
# exactly what a real business's own site would be. `normalize_origins`
# accepts `http://localhost:*` only when ENVIRONMENT=local (spec §3), which
# this seed already requires (`_refuse_outside_local`).
DEMO_WIDGET_ORIGIN = "http://localhost:5500"


def _refuse_outside_local() -> None:
    """`make seed` creates an OWNER account whose password is committed to
    this repo. That is fine against a throwaway local database and a real
    security hole against anything else, so refuse to run unless the
    environment says it is local. Override deliberately (never by default)
    with `ENVIRONMENT=local uv run python -m app.db.seed` if you truly mean
    to seed a non-local database."""
    environment = get_settings().environment
    if environment != "local":
        raise RuntimeError(
            f"refusing to seed: environment is {environment!r}, not 'local'. "
            "This creates demo@example.com / demo-password-123 as an OWNER "
            "account. If you really mean to seed this environment, override "
            "with ENVIRONMENT=local explicitly."
        )


async def _get_or_create_organization(session: AsyncSession) -> Organization:
    result = await session.execute(select(Organization).where(Organization.slug == DEMO_ORG_SLUG))
    organization = result.scalar_one_or_none()
    if organization is not None:
        return organization
    organization = Organization(id=uuid7(), name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG)
    session.add(organization)
    # Flushed before any row that references it: organizations/users/
    # memberships have no ORM relationship() between them (deliberately —
    # see AuthService.register), so the unit of work has no dependency edge
    # forcing this insert first. Each get-or-create step here flushes before
    # the next one reads its id, for the same reason.
    await session.flush()
    return organization


async def _get_or_create_user(session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.email == DEMO_EMAIL))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        id=uuid7(),
        email=DEMO_EMAIL,
        password_hash=hash_password(DEMO_PASSWORD),
        full_name="Demo Owner",
    )
    session.add(user)
    await session.flush()
    return user


async def _get_or_create_membership(
    session: AsyncSession, organization: Organization, user: User
) -> Membership:
    result = await session.execute(
        select(Membership).where(
            Membership.organization_id == organization.id,
            Membership.user_id == user.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is not None:
        return membership
    membership = Membership(
        id=uuid7(),
        organization_id=organization.id,
        user_id=user.id,
        role=MembershipRole.OWNER,
    )
    session.add(membership)
    await session.flush()
    return membership


async def _get_or_create_prompt(session: AsyncSession, tenant: TenantContext) -> Prompt:
    result = await session.execute(
        select(Prompt).where(
            Prompt.organization_id == tenant.organization_id,
            Prompt.key == DEMO_PROMPT_KEY,
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is not None:
        return prompt
    return await PromptService(session, tenant).create_prompt(
        CreatePromptInput(
            name="Sales system prompt",
            key=DEMO_PROMPT_KEY,
            description="The default grounded sales assistant prompt.",
            system_prompt=DEFAULT_SALES_SYSTEM_PROMPT,
        )
    )


def demo_agent_input() -> CreateAgentInput:
    """The demo agent's create input.

    Split out of `_get_or_create_agent` so it can be tested without a database,
    and because it is the one place `DEFAULT_LLM_PROVIDER` is still read.
    `CreateAgentInput` requires a provider and a model — deliberately, so a
    user creating an agent in the dashboard has to choose one instead of
    silently inheriting this — and the seed is the caller that genuinely wants
    the configured default: a fresh clone has no API key, so the demo agent has
    to be one that answers offline.

    The model comes from the chosen provider's own entry in `DEFAULT_MODELS`,
    never a literal: pairing `fake` with an OpenAI model id would seed a demo
    agent that cannot answer.
    """
    provider = get_settings().default_llm_provider
    return CreateAgentInput(
        name=DEMO_AGENT_NAME,
        provider=provider,
        model=DEFAULT_MODELS[provider],
    )


async def _get_or_create_agent(session: AsyncSession, tenant: TenantContext) -> Agent:
    result = await session.execute(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id,
            Agent.slug == DEMO_AGENT_SLUG,
        )
    )
    agent = result.scalar_one_or_none()
    if agent is not None:
        return agent
    return await AgentService(session, tenant).create_agent(demo_agent_input())


async def seed() -> None:
    _refuse_outside_local()

    async with untenanted_session() as session:
        organization = await _get_or_create_organization(session)
        user = await _get_or_create_user(session)
        await _get_or_create_membership(session, organization, user)
        org_id, user_id = organization.id, user.id

    tenant = TenantContext(
        organization_id=org_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="seed",
    )
    async with tenant_session(tenant) as session:
        prompt = await _get_or_create_prompt(session, tenant)
        agent = await _get_or_create_agent(session, tenant)
        if agent.prompt_id != prompt.id:
            agent.prompt_id = prompt.id
        # A freshly created agent starts `draft` (`AgentService.create_agent`),
        # and the public widget requires `active` -- `load_available`
        # (app/widget/service.py) treats a draft agent identically to an
        # unknown key, spec §4. Without this, `enabled=True` below is a widget
        # that still 404s for every visitor, which defeats the whole point of
        # a demo seed. Direct assignment, same idiom as `agent.prompt_id`
        # above -- this is the seed's own ORM object in the seed's own
        # transaction, not a caller-supplied update needing
        # `AgentService.update_agent`'s validation.
        if agent.status is not AgentStatus.ACTIVE:
            agent.status = AgentStatus.ACTIVE
        public_key = agent.public_key

        # Ruling R4 (ledger): `WidgetSettingsService.update` requires an
        # owner/admin `TenantContext` -- exactly the one built above for the
        # seeded owner, so no second session or role is needed. Idempotent
        # like every other step here: an upsert, safe to re-run.
        await WidgetSettingsService(session, tenant).update(
            agent.id,
            UpdateWidgetSettingsInput(
                enabled=True,
                allowed_origins=[DEMO_WIDGET_ORIGIN],
                brand_color="#2563eb",
                position=WidgetPosition.RIGHT,
                title=None,
                daily_message_cap=500,
            ),
        )

    print(f"seed: {DEMO_EMAIL} / {DEMO_PASSWORD} ready in {DEMO_ORG_NAME}")  # noqa: T201
    print(  # noqa: T201
        f"seed: widget demo -- run `make widget-demo`, then open "
        f"http://localhost:5500/?key={public_key}"
    )


if __name__ == "__main__":
    asyncio.run(seed())
