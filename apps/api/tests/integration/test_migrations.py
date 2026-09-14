import pytest
from sqlalchemy import text

pytestmark = pytest.mark.anyio


async def test_identity_tables_exist(owner_connection):
    result = await owner_connection.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    )
    tables = {row[0] for row in result}
    assert {"organizations", "users", "memberships"} <= tables


async def test_email_column_is_citext(owner_connection):
    """citext is what makes the unique index case-insensitive."""
    result = await owner_connection.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'email'"
        )
    )
    assert result.scalar_one() == "citext"


async def test_membership_is_unique_per_org_and_user(owner_connection):
    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'memberships' "
            "AND indexdef LIKE '%UNIQUE%organization_id, user_id%'"
        )
    )
    assert result.scalar_one() == 1


async def test_identity_tables_do_not_have_rls(owner_connection):
    """organizations/users/memberships are reached through membership joins,
    not through a tenant setting, so they are deliberately excluded from RLS."""
    result = await owner_connection.execute(
        text(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE relname = 'users' AND relnamespace = 'public'::regnamespace"
        )
    )
    assert result.scalar_one() is False


async def test_readiness_reports_database_up(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] is True
