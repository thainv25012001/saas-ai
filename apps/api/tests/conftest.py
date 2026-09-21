import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from app.core.tenancy import TenantContext

# Set before importing the app: Settings reads the environment at import time.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai",
)
os.environ.setdefault(
    "MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai",
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-please")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def owner_connection() -> AsyncIterator["AsyncConnection"]:
    """A connection as app_owner — bypasses RLS. Use it to assert on schema
    and to set up fixture data across organizations."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as connection:
        yield connection
    await engine.dispose()


@pytest.fixture
async def clean_users(owner_connection: "AsyncConnection") -> AsyncIterator[None]:
    """Auth tests share a database. Remove test rows before and after so each
    test starts from a known state, and rate-limit counters do not bleed."""
    from sqlalchemy import text

    async def _purge() -> None:
        await owner_connection.execute(text("DELETE FROM users WHERE email LIKE '%@example.com'"))
        await owner_connection.execute(
            text("DELETE FROM organizations WHERE name LIKE 'Ada Motors%'")
        )
        await owner_connection.commit()

    await _purge()
    await _flush_rate_limits()
    yield
    await _purge()


async def _flush_rate_limits() -> None:
    from app.core.redis import get_redis

    redis = get_redis()
    keys = [key async for key in redis.scan_iter("ratelimit:*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
async def tenant_a(owner_connection: "AsyncConnection") -> AsyncIterator["TenantContext"]:
    """A real organization plus a TenantContext for it, torn down after."""
    async for context in _make_tenant(owner_connection, "Tenant A"):
        yield context


@pytest.fixture
async def tenant_b(owner_connection: "AsyncConnection") -> AsyncIterator["TenantContext"]:
    async for context in _make_tenant(owner_connection, "Tenant B"):
        yield context


async def _make_tenant(
    owner_connection: "AsyncConnection", name: str
) -> AsyncIterator["TenantContext"]:
    import uuid as uuid_stdlib

    from sqlalchemy import text

    from app.core.ids import uuid7
    from app.core.tenancy import TenantContext
    from app.db.models import MembershipRole

    org_id, user_id, membership_id = uuid7(), uuid7(), uuid7()
    # Pre-existing flake, fixed here (review round 2, item 4): `org_id` is a
    # uuid7, whose first 48 bits are a millisecond timestamp -- so
    # `org_id.hex[:8]` (the first 32 bits) carries only ~65 seconds of
    # granularity (2**16 ms), not 32 bits of entropy, and any two tenant
    # fixtures alive in the same ~65-second window collided on
    # `organizations_slug_key`. This is the root cause of "phantom" test
    # failures blamed on concurrent runs several times across this phase --
    # serial runs mostly got away with it because teardown deletes the row
    # before the window recurs. `uuid.uuid4()` (stdlib, genuinely random,
    # not time-ordered) is what the suffix actually needs.
    slug = f"{name.lower().replace(' ', '-')}-{uuid_stdlib.uuid4().hex[:8]}"
    await owner_connection.execute(
        text(
            "INSERT INTO organizations (id, name, slug, plan, settings) "
            "VALUES (:id, :name, :slug, 'free', '{}')"
        ),
        {"id": org_id, "name": name, "slug": slug},
    )
    # A real users row backs user_id so any FK to users.id (e.g.
    # PromptVersion.created_by) can be populated from this fixture's
    # TenantContext without violating referential integrity.
    await owner_connection.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name) "
            "VALUES (:id, :email, 'not-a-real-hash', :full_name)"
        ),
        {
            "id": user_id,
            # Same fix as `slug` above, same reason: `user_id.hex[:8]` would
            # carry the same ~65-second-granularity collision risk on
            # `users.email`'s unique constraint.
            "email": f"{name.lower().replace(' ', '-')}-{uuid_stdlib.uuid4().hex[:8]}@example.com",
            "full_name": name,
        },
    )
    await owner_connection.execute(
        text(
            "INSERT INTO memberships (id, organization_id, user_id, role) "
            "VALUES (:id, :org_id, :user_id, 'owner')"
        ),
        {"id": membership_id, "org_id": org_id, "user_id": user_id},
    )
    await owner_connection.commit()

    yield TenantContext(
        organization_id=org_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="test",
    )

    await owner_connection.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})
    await owner_connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await owner_connection.commit()


async def enable_builtin_tool(
    owner_connection: "AsyncConnection",
    tenant: "TenantContext",
    agent_id: object,
    tool_name: str = "retrieve_knowledge",
    *,
    is_enabled: bool = True,
) -> object:
    """Give `agent_id` an org-scoped `tools` row named `tool_name`, linked
    through `agent_tools` -- the DB-backed shape `ChatService.
    _resolve_enabled_tool_names` (Task 7) actually reads, since Phase 4
    ships no dashboard UI to do this by hand yet.

    Deliberately org-scoped (`organization_id = tenant.organization_id`),
    never the global (`organization_id IS NULL`) builtin row a real
    deployment would seed once: `uq_tool_global_name` is a single shared
    namespace, so two tests both inserting a global `retrieve_knowledge` row
    back to back would collide on the unique index, or on this shared
    database's already-seeded row if `app.db.seed` or a migration data-seed
    ever adds one. An org-scoped row needs no such coordination -- it is
    unique to this org's tests, matching `uq_tool_org_name`'s own scope --
    and resolves identically: the Python `ToolRegistry` a real chat turn
    consults is keyed by `Tool.name` alone, never by which of the two scopes
    the enabling DB row came from.

    Idempotent per `(organization_id, name)`: `uq_tool_org_name` means a
    second call for the same org and tool name (e.g. linking `create_lead`
    to two different agents in the same test) would otherwise collide on
    the unique constraint, so an existing row is reused via `ON CONFLICT DO
    NOTHING` + a follow-up `SELECT` rather than assumed not to exist yet.

    No explicit teardown: both `tools.organization_id` and
    `agent_tools.organization_id` are `ON DELETE CASCADE` to
    `organizations.id` (migration 0008), so the `tenant_a`/`tenant_b`/
    `clean_users` fixtures' own organization deletes already remove these
    rows for free.
    """
    from sqlalchemy import text as _text

    from app.core.ids import uuid7

    candidate_id = uuid7()
    await owner_connection.execute(
        _text(
            "INSERT INTO tools (id, organization_id, name, type, config, is_enabled) "
            "VALUES (:id, :org, :name, 'builtin', '{}', true) "
            "ON CONFLICT ON CONSTRAINT uq_tool_org_name DO NOTHING"
        ),
        {"id": candidate_id, "org": tenant.organization_id, "name": tool_name},
    )
    tool_id = (
        await owner_connection.execute(
            _text("SELECT id FROM tools WHERE organization_id = :org AND name = :name"),
            {"org": tenant.organization_id, "name": tool_name},
        )
    ).scalar_one()
    await owner_connection.execute(
        _text(
            "INSERT INTO agent_tools (agent_id, tool_id, organization_id, is_enabled, overrides) "
            "VALUES (:agent_id, :tool_id, :org, :is_enabled, '{}')"
        ),
        {
            "agent_id": agent_id,
            "tool_id": tool_id,
            "org": tenant.organization_id,
            "is_enabled": is_enabled,
        },
    )
    await owner_connection.commit()
    return tool_id
