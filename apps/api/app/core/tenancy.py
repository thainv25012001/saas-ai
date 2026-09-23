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
async def rolled_back_tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]:
    """Like `tenant_session`, but the transaction is ALWAYS rolled back on
    exit, success or failure -- for work whose writes must not survive it.

    PHASE-6.md §2's reason this exists: an evaluation case's turn runs the
    real `ChatService.send` -- unmodified, so the agent sees exactly what
    production would show it, `create_lead` included -- and afterwards none
    of it may persist: no lead, conversation, message, tool-call or citation
    row. `app.current_org_id` is applied exactly as `tenant_session` applies
    it, in the same `is_local=true` transaction-scoped way, so RLS and every
    two-layer predicate see this session precisely as a real turn would;
    only what happens to the transaction at the end differs.

    `session.begin()` is kept, not left to autobegin, for the same reason
    `tenant_session` keeps it: one transaction for the whole block, so a
    caller that flushes repeatedly mid-block (`ChatService.send` does) never
    risks the `set_config` value being discarded by an early commit. Tools
    inside `send` additionally open their own `session.begin_nested()`
    savepoints (`app/chat/service.py::_LockedSessionTool._run_bounded`) --
    those nest inside this outer transaction exactly as they would inside
    `tenant_session`'s, and rolling the outer transaction back discards them
    regardless of whether any one of them was itself released or rolled
    back on its own.

    The rollback runs in a `finally`, not in place of re-raising: a case
    whose turn errors still needs that error to reach its caller, after the
    rollback has already happened -- so `run_evaluation_task` (Task 4) can
    record the failure rather than lose it along with everything else this
    block discards.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(tenant.organization_id)},
            )
            try:
                yield session
            finally:
                await session.rollback()


@asynccontextmanager
async def untenanted_session() -> AsyncIterator[AsyncSession]:
    """For work that happens before an organization is known: registration,
    login, and refresh. Only reaches organizations/users/memberships, which
    are deliberately not under RLS."""
    async with session_factory() as session:
        async with session.begin():
            yield session
