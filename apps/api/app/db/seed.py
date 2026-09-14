"""Idempotent development seed. Safe to run repeatedly."""

import asyncio

from sqlalchemy import select

from app.agents.schemas import CreateAgentInput
from app.agents.service import AgentService
from app.core.ids import uuid7
from app.core.security import hash_password
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import (
    Membership,
    MembershipRole,
    Organization,
    User,
)
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password-123"


async def seed() -> None:
    async with untenanted_session() as session:
        existing = await session.execute(select(User).where(User.email == DEMO_EMAIL))
        if existing.scalar_one_or_none() is not None:
            print(f"seed: {DEMO_EMAIL} already exists, nothing to do")  # noqa: T201
            return

        organization = Organization(id=uuid7(), name="Demo Motors", slug="demo-motors")
        user = User(
            id=uuid7(),
            email=DEMO_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            full_name="Demo Owner",
        )
        session.add_all([organization, user])
        # Flushed before the membership row: organizations/users/memberships
        # have no ORM relationship() between them (see AuthService.register),
        # so the unit of work has no dependency edge telling it membership
        # must insert last. Two steps gets the FK order right.
        await session.flush()
        org_id, user_id = organization.id, user.id

        session.add(
            Membership(
                id=uuid7(),
                organization_id=org_id,
                user_id=user_id,
                role=MembershipRole.OWNER,
            )
        )
        await session.flush()

    tenant = TenantContext(
        organization_id=org_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="seed",
    )
    async with tenant_session(tenant) as session:
        prompt = await PromptService(session, tenant).create_prompt(
            CreatePromptInput(
                name="Sales system prompt",
                key="sales_system",
                description="The default grounded sales assistant prompt.",
                system_prompt=DEFAULT_SALES_SYSTEM_PROMPT,
            )
        )
        agent = await AgentService(session, tenant).create_agent(
            CreateAgentInput(name="Demo Sales Agent")
        )
        agent.prompt_id = prompt.id

    print(f"seed: created {DEMO_EMAIL} / {DEMO_PASSWORD} in Demo Motors")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(seed())
