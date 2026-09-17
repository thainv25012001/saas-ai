"""message_citations

Revision ID: 0007_message_citations
Revises: 0006_documents
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0007_message_citations"
down_revision = "0006_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_citations",
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
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
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
    # organization_id leads (RLS and every service-layer filter key off it),
    # message_id second: "citations for this message" -- the read pattern
    # this table actually serves -- is then a left-prefix lookup rather than
    # a second, separate index.
    op.create_index(
        "ix_message_citations_organization_id_message_id",
        "message_citations",
        ["organization_id", "message_id"],
    )
    enable_rls(op, "message_citations")


def downgrade() -> None:
    disable_rls(op, "message_citations")
    op.drop_table("message_citations")
