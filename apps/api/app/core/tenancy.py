import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MembershipRole
from app.db.session import session_factory


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who is acting, and on behalf of which organization.

    Always derived on the server from an authenticated token or from the
    conversation's agent. Never from client-supplied or model-supplied input.
    """

    organization_id: uuid.UUID
    user_id: uuid.UUID | None
    role: MembershipRole | None
    request_id: str


@asynccontextmanager
async def tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]:
    """Open a transaction with the tenant setting applied.

    set_config(..., is_local=true) is transaction-scoped, so the value is
    discarded when the transaction ends and cannot survive on a pooled
    connection into somebody else's request. Everything inside the block runs
    in one transaction: committing mid-block would drop the setting.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(tenant.organization_id)},
            )
            yield session


@asynccontextmanager
async def untenanted_session() -> AsyncIterator[AsyncSession]:
    """For work that happens before an organization is known: registration,
    login, and refresh. Only reaches organizations/users/memberships, which
    are deliberately not under RLS."""
    async with session_factory() as session:
        async with session.begin():
            yield session
