import dataclasses

import pytest
from sqlalchemy import text

from app.agents.service import AgentService
from app.api_keys.service import MAX_ACTIVE_KEYS_PER_AGENT, ApiKeyService, resolve_api_key
from app.api_keys.tokens import hash_token
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.db.session import session_factory
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _agent(session, tenant):
    return await AgentService(session, tenant).create_agent(agent_input("Sales Bot"))


def _member(tenant: TenantContext) -> TenantContext:
    """`tenant_a`/`tenant_b` (tests/conftest.py) always carry `role=OWNER`.
    `TenantContext` is a frozen dataclass and `role` is checked purely in
    Python (never by RLS, which only ever reads `organization_id`), so a
    member-role context for the same organization needs no second
    membership row in the database -- just a copy with `role` swapped."""
    return dataclasses.replace(tenant, role=MembershipRole.MEMBER)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_returns_a_token_whose_plaintext_appears_in_no_column(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    assert created.token.startswith("sa_mcp_")
    assert created.api_key.key_hash == hash_token(created.token)
    assert created.api_key.key_prefix == created.token[:15]

    async with tenant_session(tenant_a) as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM api_keys WHERE id = :id"), {"id": created.api_key.id}
                )
            )
            .mappings()
            .one()
        )
    for column, value in row.items():
        if value is None:
            continue
        rendered = value.hex() if isinstance(value, bytes | bytearray) else str(value)
        assert created.token not in rendered, f"plaintext token leaked into column {column!r}"


async def test_create_strips_and_bounds_the_name(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "  Spaced  ")
    assert created.api_key.name == "Spaced"


async def test_create_rejects_a_blank_name(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        with pytest.raises(ValidationError):
            await ApiKeyService(session, tenant_a).create(agent.id, "   ")


async def test_create_rejects_a_name_over_100_characters(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        with pytest.raises(ValidationError):
            await ApiKeyService(session, tenant_a).create(agent.id, "x" * 101)


async def test_create_for_another_tenants_agent_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ApiKeyService(session, tenant_b).create(agent.id, "MCP key")


async def test_member_role_cannot_create_a_key(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    member = _member(tenant_a)
    async with tenant_session(member) as session:
        with pytest.raises(PermissionDeniedError):
            await ApiKeyService(session, member).create(agent.id, "MCP key")


async def test_the_eleventh_active_key_hits_the_cap(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ApiKeyService(session, tenant_a)
        for i in range(MAX_ACTIVE_KEYS_PER_AGENT):
            await service.create(agent.id, f"Key {i}")

        with pytest.raises(ConflictError):
            await service.create(agent.id, "One too many")


async def test_a_revoked_key_frees_a_slot(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ApiKeyService(session, tenant_a)
        keys = [
            await service.create(agent.id, f"Key {i}") for i in range(MAX_ACTIVE_KEYS_PER_AGENT)
        ]
        await service.revoke(keys[0].api_key.id)

        # The cap counts `revoked_at IS NULL` keys, so the revoke above must
        # have freed a slot -- this must succeed, not raise ConflictError.
        await service.create(agent.id, "Replacement key")


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


async def test_revoke_is_idempotent(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    async with tenant_session(tenant_a) as session:
        first = await ApiKeyService(session, tenant_a).revoke(created.api_key.id)
    async with tenant_session(tenant_a) as session:
        second = await ApiKeyService(session, tenant_a).revoke(created.api_key.id)

    assert first.revoked_at is not None
    assert first.revoked_at == second.revoked_at


async def test_member_role_cannot_revoke_a_key(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    member = _member(tenant_a)
    async with tenant_session(member) as session:
        with pytest.raises(PermissionDeniedError):
            await ApiKeyService(session, member).revoke(created.api_key.id)


async def test_revoke_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ApiKeyService(session, tenant_b).revoke(created.api_key.id)


# ---------------------------------------------------------------------------
# list_for_agent
# ---------------------------------------------------------------------------


async def test_member_role_can_list_keys(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    member = _member(tenant_a)
    async with tenant_session(member) as session:
        [listed] = await ApiKeyService(session, member).list_for_agent(agent.id)
    assert listed.name == "MCP key"


async def test_list_for_agent_is_newest_first_and_includes_revoked(tenant_a):
    """Each create/revoke runs in its own `tenant_session` (its own
    transaction) deliberately: Postgres's `now()` -- what `created_at`'s
    `server_default` uses -- is the *transaction* start time, not the
    statement time, so two inserts sharing one transaction would tie on
    `created_at` and this ordering assertion would be testing nothing."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        first = await ApiKeyService(session, tenant_a).create(agent.id, "First")
    async with tenant_session(tenant_a) as session:
        await ApiKeyService(session, tenant_a).create(agent.id, "Second")
    async with tenant_session(tenant_a) as session:
        await ApiKeyService(session, tenant_a).revoke(first.api_key.id)

    async with tenant_session(tenant_a) as session:
        listed = await ApiKeyService(session, tenant_a).list_for_agent(agent.id)

    assert [key.name for key in listed] == ["Second", "First"]
    assert listed[0].revoked_at is None
    assert listed[1].revoked_at is not None


async def test_list_for_agent_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ApiKeyService(session, tenant_b).list_for_agent(agent.id)


# ---------------------------------------------------------------------------
# touch_last_used
# ---------------------------------------------------------------------------


async def test_touch_last_used_writes_once_within_a_minute(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    async with tenant_session(tenant_a) as session:
        service = ApiKeyService(session, tenant_a)

        await service.touch_last_used(created.api_key.id)
        after_first = (
            await session.execute(
                text("SELECT last_used_at FROM api_keys WHERE id = :id"),
                {"id": created.api_key.id},
            )
        ).scalar_one()
        assert after_first is not None

        # Same transaction, so `now()` is identical to the first touch's --
        # the WHERE clause's own throttle, not clock skew, is what must
        # prevent this second write.
        await service.touch_last_used(created.api_key.id)
        after_second = (
            await session.execute(
                text("SELECT last_used_at FROM api_keys WHERE id = :id"),
                {"id": created.api_key.id},
            )
        ).scalar_one()
        assert after_second == after_first


# ---------------------------------------------------------------------------
# resolve_api_key
# ---------------------------------------------------------------------------


async def test_resolve_api_key_round_trips(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    resolved = await resolve_api_key(created.token)
    assert resolved is not None
    assert resolved.api_key_id == created.api_key.id
    assert resolved.organization_id == tenant_a.organization_id
    assert resolved.agent_id == agent.id


async def test_resolve_api_key_returns_none_for_a_wrong_token(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    assert await resolve_api_key("sa_mcp_" + "x" * 43) is None


async def test_resolve_api_key_returns_none_for_a_malformed_token():
    assert await resolve_api_key("not-a-token") is None
    assert await resolve_api_key("") is None
    assert await resolve_api_key("sa_mcp_short") is None


async def test_resolve_api_key_returns_none_after_revoke(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    async with tenant_session(tenant_a) as session:
        await ApiKeyService(session, tenant_a).revoke(created.api_key.id)

    assert await resolve_api_key(created.token) is None


async def test_resolve_api_key_works_as_app_user_with_no_tenant_set(tenant_a):
    """The whole point of the `resolve_api_key` SQL function (docs/PHASE-7.md
    §3): a plain `SELECT` on `api_keys` from `app_user` with no tenant set
    sees nothing, because RLS applies to `app_user` and this table's policy
    (`enable_rls`, not `FORCE`d) admits no row without `app.current_org_id`
    -- but the function itself, `SECURITY DEFINER` and owned by the
    migrating role, is not subject to that policy and finds the key anyway.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        created = await ApiKeyService(session, tenant_a).create(agent.id, "MCP key")

    async with session_factory() as session:
        async with session.begin():
            plain_select = await session.execute(
                text("SELECT * FROM api_keys WHERE id = :id"), {"id": created.api_key.id}
            )
            assert plain_select.first() is None

    resolved = await resolve_api_key(created.token)
    assert resolved is not None
    assert resolved.api_key_id == created.api_key.id
    assert resolved.agent_id == agent.id


async def test_resolve_api_key_function_returns_nothing_for_null_or_empty_hash():
    """It cannot be used to enumerate keys: an exact hash is required, and a
    degenerate NULL or empty-bytes value matches no row rather than every
    row or raising."""
    async with session_factory() as session:
        async with session.begin():
            null_result = await session.execute(text("SELECT * FROM resolve_api_key(NULL)"))
            assert null_result.first() is None

            empty_result = await session.execute(
                text("SELECT * FROM resolve_api_key(:hash)"), {"hash": b""}
            )
            assert empty_result.first() is None
