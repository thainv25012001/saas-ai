import uuid

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session

pytestmark = pytest.mark.anyio


@pytest.fixture
async def two_orgs(owner_connection):
    """Two organizations, each with one row in the RLS probe table."""
    org_a, org_b = uuid7(), uuid7()
    for org_id, name in ((org_a, "Org A"), (org_b, "Org B")):
        await owner_connection.execute(
            text(
                "INSERT INTO organizations (id, name, slug, plan, settings) "
                "VALUES (:id, :name, :slug, 'free', '{}')"
            ),
            {"id": org_id, "name": name, "slug": name.lower().replace(" ", "-")},
        )
        await owner_connection.execute(
            text("INSERT INTO rls_probe (id, organization_id, label) VALUES (:id, :org, :label)"),
            {"id": uuid7(), "org": org_id, "label": f"{name} secret"},
        )
    await owner_connection.commit()
    yield org_a, org_b
    await owner_connection.execute(
        text("DELETE FROM organizations WHERE id IN (:a, :b)"),
        {"a": org_a, "b": org_b},
    )
    await owner_connection.commit()


def _context(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(organization_id=org_id, user_id=None, role=None, request_id="test")


async def test_tenant_sees_only_its_own_rows(two_orgs):
    org_a, _org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        result = await session.execute(text("SELECT label FROM rls_probe"))
        labels = [row[0] for row in result]
    assert labels == ["Org A secret"]


async def test_tenant_cannot_see_the_other_tenants_rows(two_orgs):
    org_a, org_b = two_orgs
    async with tenant_session(_context(org_b)) as session:
        result = await session.execute(text("SELECT label FROM rls_probe"))
        labels = [row[0] for row in result]
    assert "Org A secret" not in labels


async def test_targeting_another_tenants_row_by_id_returns_nothing(two_orgs):
    """Even with the exact primary key, the row is invisible."""
    org_a, org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        row_id = (await session.execute(text("SELECT id FROM rls_probe"))).scalar_one()

    async with tenant_session(_context(org_b)) as session:
        result = await session.execute(
            text("SELECT label FROM rls_probe WHERE id = :id"), {"id": row_id}
        )
        assert result.first() is None


async def test_insert_for_another_tenant_is_rejected(two_orgs):
    """A tenant cannot write a row it would not be able to read: the policy
    rejects the cross-tenant insert rather than silently scoping it."""
    from sqlalchemy.exc import DBAPIError

    org_a, org_b = two_orgs
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(_context(org_a)) as session:
            await session.execute(
                text(
                    "INSERT INTO rls_probe (id, organization_id, label) "
                    "VALUES (:id, :org, 'smuggled')"
                ),
                {"id": uuid7(), "org": org_b},
            )


async def test_update_cannot_move_a_row_to_another_tenant(two_orgs):
    from sqlalchemy.exc import DBAPIError

    org_a, org_b = two_orgs
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(_context(org_a)) as session:
            await session.execute(
                text("UPDATE rls_probe SET organization_id = :org"), {"org": org_b}
            )


async def test_setting_does_not_leak_between_sessions(two_orgs):
    """SET LOCAL dies with the transaction. If it leaked through the pool,
    a later request could inherit a previous tenant's context.

    Deliberately does NOT read the setting back through another
    tenant_session() call for org_b: that call would itself re-set the
    value, which would make this test pass even if the underlying
    set_config used session scope (is_local=false) instead of transaction
    scope. Instead it opens a bare, un-tenanted session on the same pool
    and checks that org_a's setting did not survive past its transaction's
    commit.

    `current != str(org_a)` alone would also pass if the pool simply handed
    back a *different*, never-used backend (where the setting is NULL) --
    that would prove nothing about leaking. pg_backend_pid() pins both
    blocks to the same physical backend, so the second assertion is
    actually exercising this connection's post-commit state, not a fresh
    one. And per NULLIF's contract (see enable_rls), a warm backend whose
    tenant transaction has ended reports '' -- not NULL -- so that is the
    literal value asserted here, rather than a looser not-equal check.
    """
    from app.db.session import session_factory

    org_a, _org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        pid_during = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()

    async with session_factory() as session:
        async with session.begin():
            pid_after = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            current = (
                await session.execute(text("SELECT current_setting('app.current_org_id', true)"))
            ).scalar_one()
    assert pid_after == pid_during
    assert current == ""


async def test_missing_tenant_setting_yields_no_rows(two_orgs):
    """An unset context must be an empty result, never an error and never
    every row -- including on a connection that has already served a
    tenant request.

    A virgin backend that has never run SET LOCAL app.current_org_id
    reports it as NULL. But once any transaction on a backend has set it,
    the custom GUC placeholder does not revert to NULL when that
    transaction ends -- it reverts to the empty string. A pooled
    connection is exactly this: warm, not virgin. So this test
    deliberately runs a tenant_session first, on the same pooled
    connection, before performing the untenanted read.
    """
    from app.db.session import session_factory

    org_a, _org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        await session.execute(text("SELECT 1"))

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(text("SELECT label FROM rls_probe"))
            assert result.all() == []
