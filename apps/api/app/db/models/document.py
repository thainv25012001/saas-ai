import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentSourceType(enum.StrEnum):
    UPLOAD = "upload"
    URL = "url"
    TEXT = "text"


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Document(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A source a business uploaded for its assistant to answer from.

    `checksum` backs `DocumentService.find_by_checksum`: re-uploading the
    same file should be recognised as a dedup, not silently re-ingested and
    re-embedded under a second id.
    """

    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[DocumentSourceType] = mapped_column(
        SAEnum(
            DocumentSourceType,
            name="document_source_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentChunk(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One retrievable slice of a document's text, plus its embedding.

    `content_tsv` (keyword search) is a database-generated column created
    only in the migration -- it has no ORM-mapped counterpart here because
    nothing in Python ever writes to it; Task 6's hybrid retrieval reads it
    directly with `websearch_to_tsquery('english', ...)`.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1536 matches both embedders in app/embeddings/ (HashingEmbedder and
    # OpenAIEmbeddingProvider) exactly. pgvector fixes a column's dimension at
    # creation, so a mismatch here would mean an ALTER TABLE against live
    # data later rather than a one-line fix now.
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
