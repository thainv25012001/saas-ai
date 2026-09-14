"""conversations, messages, and usage_events

Revision ID: 0005_conversations
Revises: 0004_prompts
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0005_conversations"
down_revision = "0004_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
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
        sa.Column("visitor_id", sa.String(255), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("playground", "widget", "api", name="conversation_channel"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("open", "closed", name="conversation_status"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_conversations_organization_id", "conversations", ["organization_id"])
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])
    enable_rls(op, "conversations")

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("user", "assistant", "system", "tool", name="message_role"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_blocks", postgresql.JSONB(), nullable=True),
        sa.Column(
            "prompt_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompt_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        # Numeric(12, 6), not Float: cost_usd must round-trip as an exact
        # Decimal, and stays NULL for an unpriced model instead of 0.
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(50), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
    )
    op.create_index("ix_messages_organization_id", "messages", ["organization_id"])
    enable_rls(op, "messages")

    op.create_table(
        "usage_events",
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
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.Enum("llm", "embedding", name="usage_kind"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_usage_events_organization_id", "usage_events", ["organization_id"])
    enable_rls(op, "usage_events")


def downgrade() -> None:
    disable_rls(op, "usage_events")
    op.drop_table("usage_events")
    op.execute("DROP TYPE IF EXISTS usage_kind")

    disable_rls(op, "messages")
    op.drop_table("messages")
    op.execute("DROP TYPE IF EXISTS message_role")

    disable_rls(op, "conversations")
    op.drop_table("conversations")
    op.execute("DROP TYPE IF EXISTS conversation_status")
    op.execute("DROP TYPE IF EXISTS conversation_channel")
