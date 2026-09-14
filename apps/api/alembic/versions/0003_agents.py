"""agents and agent_configs

Revision ID: 0003_agents
Revises: 0002a_rls_null_guard
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0003_agents"
down_revision = "0002a_rls_null_guard"
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


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "disabled", name="agent_status"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("provider", sa.String(50), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(100), nullable=False, server_default="gpt-4o-mini"),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.3"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="1024"),
        # No foreign key yet: `prompts` doesn't exist until Task 8 creates
        # it. Task 8 owes an `op.create_foreign_key(...)` here to match the
        # `ForeignKey(...)` it restores on the Agent.prompt_id ORM column
        # (see app/db/models/agent.py) -- both sides need updating together.
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("public_key", sa.String(64), nullable=False, unique=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("organization_id", "slug", name="uq_agent_org_slug"),
    )
    op.create_index("ix_agents_organization_id", "agents", ["organization_id"])
    enable_rls(op, "agents")

    op.create_table(
        "agent_configs",
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
            unique=True,
        ),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(50), nullable=False, server_default="friendly"),
        sa.Column("language", sa.String(20), nullable=False, server_default="en"),
        sa.Column("greeting", sa.Text(), nullable=True),
        sa.Column("fallback_message", sa.Text(), nullable=False),
        sa.Column(
            "enabled_tool_names",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("retrieval_min_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("max_agent_steps", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("guardrails", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
    )
    op.create_index("ix_agent_configs_organization_id", "agent_configs", ["organization_id"])
    enable_rls(op, "agent_configs")


def downgrade() -> None:
    disable_rls(op, "agent_configs")
    op.drop_table("agent_configs")
    disable_rls(op, "agents")
    op.drop_table("agents")
    op.execute("DROP TYPE IF EXISTS agent_status")
