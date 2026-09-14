import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UUIDPrimaryKeyMixin


class ConversationChannel(enum.StrEnum):
    PLAYGROUND = "playground"
    WIDGET = "widget"
    API = "api"


class ConversationStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class UsageKind(enum.StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"


class Conversation(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """A conversation thread with one agent.

    No TimestampMixin here: `started_at`/`last_message_at`/`closed_at` are
    the domain timestamps the spec (ARCHITECTURE.md §3.5) actually calls
    for, and a generic `created_at`/`updated_at` pair would just duplicate
    `started_at` while adding nothing `updated_at`'s onupdate could
    meaningfully track.
    """

    __tablename__ = "conversations"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    visitor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel: Mapped[ConversationChannel] = mapped_column(
        SAEnum(
            ConversationChannel,
            name="conversation_channel",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    status: Mapped[ConversationStatus] = mapped_column(
        SAEnum(
            ConversationStatus,
            name="conversation_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ConversationStatus.OPEN,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """A single turn in a conversation.

    Import this as `ConversationMessage` in any module that also needs
    `app.llm.types.Message` (the LLM wire type) in scope — see
    `app/db/models/__init__.py`, which exports both names for this class.

    Append-only: rows are written once by `ConversationService.append_message`
    and never edited afterwards, so unlike `Agent`/`Prompt` there is no
    `updated_at` to maintain — only `created_at`.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(
            MessageRole,
            name="message_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_blocks: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nullable, not zero: an unpriced model must record NULL so a missing
    # price is visible, rather than a float/zero that silently understates
    # the bill. Numeric(12, 6), not Float, so `Decimal("0.001234")` round-trips
    # exactly instead of drifting through binary floating point.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UsageEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """The substrate billing would later read — not billing itself.

    Append-only, like `Message`: `created_at` only, no `updated_at`.
    """

    __tablename__ = "usage_events"

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[UsageKind] = mapped_column(
        SAEnum(
            UsageKind,
            name="usage_kind",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
