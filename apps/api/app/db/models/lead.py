import enum
import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class LeadStatus(enum.StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


class Lead(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A prospect an agent captured mid-conversation. This task only owns the
    storage shape; the tool that writes these rows is later work.

    `product_id` is nullable and deliberately carries no foreign key: §3.4's
    `products` table does not exist yet (a later phase), so this column is a
    forward reference recorded now to avoid an `ALTER TABLE` against live
    lead data once it does -- the same reasoning `document_chunks.embedding`'s
    fixed width and the `UsageEvent` "write it now" comment already apply
    elsewhere in this schema.

    `name`/`email`/`phone`/`interest` are all nullable: a real chat rarely
    yields a complete contact card in one turn, and a tool call that only
    captured a phone number this message is still worth a row. Requiring all
    four up front would make that ordinary partial case impossible to
    represent.
    """

    __tablename__ = "leads"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    interest: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[LeadStatus] = mapped_column(
        SAEnum(LeadStatus, name="lead_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=LeadStatus.NEW,
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
