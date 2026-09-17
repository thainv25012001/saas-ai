import uuid

from sqlalchemy import Float, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class MessageCitation(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Which retrieved passage(s) grounded one assistant message, and where
    each ranked in `RetrievalService`'s fused ordering.

    Written once, by `ChatService._record_citations`, in the same
    transaction as the assistant message it cites -- append-only like
    `Message`/`UsageEvent`, so `updated_at` (from `TimestampMixin`) is never
    touched after insert; carried anyway for consistency with every other
    table in the schema (see the identical note on `Message`).

    `document_id` duplicates information already reachable via
    `chunk_id -> document_chunks.document_id`, but is stored directly so a
    reader of this table (the SSE payload builder, a future citations query)
    never has to join through `document_chunks` just to know which document
    a citation names.
    """

    __tablename__ = "message_citations"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
