"""documents and document_chunks

Revision ID: 0006_documents
Revises: 0005_conversations
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0006_documents"
down_revision = "0005_conversations"
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
    # Installed locally by infrastructure/postgres/init.sql, but a managed
    # database (Neon, RDS, ...) starts bare -- IF NOT EXISTS makes this
    # self-provisioning wherever the migration role has the privilege, and a
    # no-op everywhere else.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("upload", "url", "text", name="document_source_type"),
            nullable=False,
        ),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "ready", "failed", name="document_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_TIMESTAMPS,
    )
    # One composite index, not two: organization_id leads (matching this
    # codebase's convention and serving every organization_id-only filter as
    # a left prefix), and find_by_checksum -- the dedup check that runs on
    # every upload -- is the hot lookup that needs checksum in the index at
    # all. Added now, while the table is empty, because adding it later
    # means an ALTER TABLE against live data instead of a free line here.
    op.create_index(
        "ix_documents_organization_id_checksum", "documents", ["organization_id", "checksum"]
    )
    enable_rls(op, "documents")

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        # 1536 dimensions, matching HashingEmbedder and OpenAIEmbeddingProvider
        # exactly (see app/embeddings/). pgvector fixes a column's width at
        # creation, so a mismatch here means an ALTER TABLE against live
        # data, not a one-line fix, if it is ever caught late.
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        # Generated, not maintained from Python: Postgres recomputes this on
        # every insert/update of `content`, so it can never drift out of
        # sync with the text it indexes. The 'english' configuration is
        # deliberate -- Task 6's retrieval calls
        # `websearch_to_tsquery('english', ...)` against this column, and
        # 'simple' (or the server's default config) would still populate
        # this column and pass a naive "is it non-empty" check while
        # matching zero rows at query time.
        sa.Column(
            "content_tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
    )
    # One composite index, not two: RLS and every service-layer query filter
    # on organization_id, and replace_chunks/cascade deletes join on
    # document_id, so (organization_id, document_id) with organization_id
    # leading serves both as a left prefix.
    op.create_index(
        "ix_document_chunks_organization_id_document_id",
        "document_chunks",
        ["organization_id", "document_id"],
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_content_tsv ON document_chunks USING gin (content_tsv)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )
    enable_rls(op, "document_chunks")


def downgrade() -> None:
    disable_rls(op, "document_chunks")
    op.drop_table("document_chunks")

    disable_rls(op, "documents")
    op.drop_table("documents")
    op.execute("DROP TYPE IF EXISTS document_status")
    op.execute("DROP TYPE IF EXISTS document_source_type")
