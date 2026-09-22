"""tools, agent_tools, message_tool_calls and leads

Revision ID: 0008_tools_and_leads
Revises: 0007_message_citations
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0008_tools_and_leads"
down_revision = "0007_message_citations"
branch_labels = None
depends_on = None

_TIMESTAMPS = (
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
)

# Same guard `enable_rls` uses: NULLIF collapses the empty-string placeholder
# a warmed, previously-tenanted pooled connection reports back to NULL
# before the cast, so both a virgin and a warm backend fail closed.
_GUARDED_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def _enable_tools_rls() -> None:
    """`tools.organization_id` is the one nullable tenant column in this
    schema: NULL means a global builtin every organization must see. The
    plain `enable_rls` policy (`organization_id = current_org`) is wrong
    here -- SQL's three-valued logic makes a NULL row satisfy neither side
    of that equality (NULL compared to anything is UNKNOWN, never TRUE), so
    applied as-is every builtin becomes invisible to every tenant and no
    agent can call `retrieve_knowledge`. This table therefore gets its own
    `tenant_isolation` policy: same name, so it still satisfies
    test_migrations.py's parametrized check and reads the same as every
    other table's, but a predicate that also admits `organization_id IS
    NULL`.

    WITH CHECK deliberately does *not* mirror USING here, unlike every other
    policy in this migration (agent_tools/message_tool_calls/leads all use
    the ordinary symmetric `enable_rls`). USING must keep admitting NULL so
    every tenant can still read global builtins; WITH CHECK must not, or a
    tenant session could INSERT/UPDATE a NULL-org row of its own, and that
    same NULL branch on USING would then show it to every *other*
    organization too -- a tenant-created row masquerading as a builtin.
    Builtins are seeded by the owner role, which bypasses RLS entirely, so
    no legitimate write ever needs the NULL branch on the WITH CHECK side.
    """
    op.execute("ALTER TABLE tools ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tools "
        f"USING (organization_id = {_GUARDED_ORG} OR organization_id IS NULL) "
        f"WITH CHECK (organization_id = {_GUARDED_ORG})"
    )


def upgrade() -> None:
    op.create_table(
        "tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable -- see _enable_tools_rls above. No ondelete="CASCADE" data
        # loss risk in practice: an organization is never deleted while it
        # still owns tool rows in this phase, but CASCADE is still correct
        # once it can be, same as every other organization_id FK here.
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "type",
            sa.Enum("builtin", "mcp", "http", name="tool_type"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        *_TIMESTAMPS,
        # Postgres treats NULL as distinct from NULL for uniqueness, so this
        # constraint only ever fires for two org-scoped rows -- it does
        # nothing to stop two builtins sharing a name. That second case is
        # ix_tools_uq_global_name below. See the Tool docstring for why both
        # exist: ToolRegistry resolves by name into a dict, and a collision
        # would silently drop one row's config, not error.
        sa.UniqueConstraint("organization_id", "name", name="uq_tool_org_name"),
    )
    op.create_index("ix_tools_organization_id", "tools", ["organization_id"])
    op.create_index(
        "uq_tool_global_name",
        "tools",
        ["name"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    _enable_tools_rls()

    op.create_table(
        "agent_tools",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tools.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("overrides", postgresql.JSONB(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
    )
    # agent_id already leads the primary key, so a second index starting
    # with it would only duplicate that. organization_id is what RLS filters
    # on and the primary key does not cover it at all, so it gets its own.
    op.create_index("ix_agent_tools_organization_id", "agent_tools", ["organization_id"])
    enable_rls(op, "agent_tools")

    op.create_table(
        "message_tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(100), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        *_TIMESTAMPS,
    )
    # organization_id leads (RLS, and every service-layer filter key off it),
    # message_id second: "tool calls for this message" is the read pattern
    # the SSE payload builder and any future transcript view actually need,
    # matching the identical index shape on message_citations in 0007.
    op.create_index(
        "ix_message_tool_calls_organization_id_message_id",
        "message_tool_calls",
        ["organization_id", "message_id"],
    )
    enable_rls(op, "message_tool_calls")

    op.create_table(
        "leads",
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
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("interest", sa.Text(), nullable=True),
        # No FK: §3.4's `products` table does not exist yet (a later phase).
        # See the note on app/db/models/lead.py::Lead.
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("new", "contacted", "qualified", "won", "lost", name="lead_status"),
            nullable=False,
            server_default="new",
        ),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
    )
    # organization_id leads; agent_id second because "leads for this agent"
    # is the dashboard's primary listing (docs/ARCHITECTURE.md §4's
    # dashboard/leads/ route), served as a left prefix the same way
    # document_chunks' (organization_id, document_id) index serves its own
    # primary lookup.
    op.create_index(
        "ix_leads_organization_id_agent_id",
        "leads",
        ["organization_id", "agent_id"],
    )
    enable_rls(op, "leads")


def downgrade() -> None:
    disable_rls(op, "leads")
    op.drop_table("leads")
    op.execute("DROP TYPE IF EXISTS lead_status")

    disable_rls(op, "message_tool_calls")
    op.drop_table("message_tool_calls")

    disable_rls(op, "agent_tools")
    op.drop_table("agent_tools")

    disable_rls(op, "tools")
    op.drop_table("tools")
    op.execute("DROP TYPE IF EXISTS tool_type")
