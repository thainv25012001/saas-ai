"""gmail maintenance resolver

Revision ID: 0019_gmail_maintenance_resolver
Revises: 0018_supplier_mapping_reviews
"""

from alembic import op
from app.db.base import runtime_role

revision = "0019_gmail_maintenance_resolver"
down_revision = "0018_supplier_mapping_reviews"
branch_labels = None
depends_on = None

_LIST_GMAIL_CONNECTIONS_FUNCTION = """
CREATE FUNCTION list_gmail_connections_for_maintenance()
RETURNS TABLE (
    organization_id uuid,
    connection_id uuid,
    email text,
    watch_expires_at timestamptz,
    history_id text,
    last_history_sync_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT
        connection.organization_id,
        connection.id,
        connection.email,
        connection.watch_expires_at,
        connection.history_id,
        connection.last_history_sync_at
    FROM public.gmail_connections AS connection
    WHERE connection.status = 'connected'
    ORDER BY connection.watch_expires_at NULLS FIRST,
             connection.organization_id,
             connection.id
$$;
"""


def upgrade() -> None:
    op.execute(_LIST_GMAIL_CONNECTIONS_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION list_gmail_connections_for_maintenance() FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION list_gmail_connections_for_maintenance() TO {runtime_role()}"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS list_gmail_connections_for_maintenance()")
