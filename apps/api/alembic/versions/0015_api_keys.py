"""api_keys

Revision ID: 0015_api_keys
Revises: 0014_evaluations

Task 2 (docs/PHASE-7.md §3): the credential an MCP client authenticates
with, bound to exactly one agent. `api_keys` is tenant-owned like every
other table (`enable_rls`), but authentication is the one thing that must
happen *before* the organization is known -- that is what the key is for.
So this migration also creates `resolve_api_key`, a `SECURITY DEFINER`
function owned by the migrating role (the table's owner, and so not itself
subject to the table's un-FORCEd policy -- see `app.db.base.enable_rls`'s
docstring for why table ownership already means that): it takes one exact
hash and returns three ids, nothing else. `EXECUTE` is revoked from
`PUBLIC` and granted only to `app_user`, and `search_path` is pinned so it
cannot be redirected by a session-level `search_path` change.

`scopes text[]` from ARCHITECTURE.md §3.1 is deliberately not here
(docs/PHASE-7.md §2, §3, marked ★): the tools an MCP caller can reach are
exactly the agent's own `agent_tools` grants, so a second permission model
on the key itself would only be a second thing to keep in sync with the
first.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0015_api_keys"
down_revision = "0014_evaluations"
branch_labels = None
depends_on = None

_RESOLVE_API_KEY_FUNCTION = """
CREATE FUNCTION resolve_api_key(p_hash bytea)
RETURNS TABLE (api_key_id uuid, organization_id uuid, agent_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$ SELECT id, organization_id, agent_id FROM api_keys
      WHERE key_hash = p_hash AND revoked_at IS NULL $$;
"""


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        # The token itself is never stored -- only this hash. See
        # app.api_keys.tokens.hash_token.
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    # `ApiKeyService.list_for_agent`'s own access pattern (and every other
    # per-agent read/write here): organization_id first, matching RLS's own
    # predicate, agent_id second.
    op.create_index(
        "ix_api_keys_organization_id_agent_id", "api_keys", ["organization_id", "agent_id"]
    )
    enable_rls(op, "api_keys")

    op.execute(_RESOLVE_API_KEY_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_api_key(bytea) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_api_key(bytea) TO app_user")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_api_key(bytea)")
    disable_rls(op, "api_keys")
    op.drop_table("api_keys")
