import uuid
from collections.abc import AsyncIterator, Sequence

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext

from app.auth.dependencies import tenant_from_bearer
from app.core.errors import AuthenticationError
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import AgentConfig


class Context(BaseContext):
    """Per-request state. One tenant-bound session for the whole operation,
    so every resolver in a query shares one transaction and one RLS setting.

    `tenant` and `session` are nullable because an unauthenticated request
    still needs a context object: raising during context construction would
    let FastAPI's exception handler return a bare 401 instead of a properly
    shaped GraphQL error. Resolvers call `_require_tenant` instead.

    Inherits `strawberry.fastapi.BaseContext` because current
    strawberry-graphql refuses a custom context object that is not a
    `BaseContext` subclass (or a plain dict) — see `GraphQLRouter`'s
    context-merging dependency.
    """

    def __init__(self, tenant: TenantContext | None, session: AsyncSession | None) -> None:
        super().__init__()
        self.tenant = tenant
        self.session = session
        self.config_loader: DataLoader[uuid.UUID, AgentConfig | None] | None = (
            DataLoader(load_fn=self._load_configs) if session is not None else None
        )

    async def _load_configs(self, agent_ids: Sequence[uuid.UUID]) -> list[AgentConfig | None]:
        """Batches `agents { config { ... } }` into one query instead of one
        per agent. Without this, listing 50 agents issues 51 queries."""
        assert self.session is not None
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.agent_id.in_(list(agent_ids)))
        )
        by_agent = {config.agent_id: config for config in result.scalars().all()}
        return [by_agent.get(agent_id) for agent_id in agent_ids]


async def build_context(request: Request) -> AsyncIterator[Context]:
    """A generator dependency: FastAPI holds it open for the whole request, so
    the tenant session and its transaction stay alive while resolvers run."""
    try:
        tenant = tenant_from_bearer(request)
    except AuthenticationError:
        yield Context(tenant=None, session=None)
        return

    async with tenant_session(tenant) as session:
        yield Context(tenant, session)
