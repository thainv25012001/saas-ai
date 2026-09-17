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
        # SET NULL, not CASCADE, and therefore nullable. A citation is a
        # record of what an answer was grounded on at the moment it was
        # given, and `docs/PHASE-3.md` §5 is explicit that the reason this
        # table exists is to make "did it answer from the sources?"
        # answerable about turns that have already happened. Under CASCADE
        # it was not: `DocumentService.replace_chunks` deletes every chunk
        # before inserting the new ones, so re-ingesting a document erased
        # the history of every answer it ever grounded while the assistant
        # messages themselves survived -- and `deleteDocument`, which is
        # reachable from the dashboard today, did the same thing through
        # `document_chunks`' own cascade.
        #
        # NULL here therefore means "the passage this cited no longer
        # exists", which is a true and useful thing for a reader to learn,
        # where a missing row taught it nothing at all. The two denormalised
        # columns below are what keep the citation legible once that
        # happens.
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Copied onto the citation rather than joined for, so the row still
        # says what was cited after the chunk and the document are both
        # gone. `excerpt` is the same preview the SSE `citations` event
        # carried (see `ChatService._excerpt`), not the whole chunk: enough
        # to recognise the passage, not a second copy of the corpus.
        sa.Column("document_title", sa.String(255), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
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
