from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class GmailConnection(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One organization's Gmail mailbox connection; tokens are ciphertext only."""

    __tablename__ = "gmail_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_gmail_connections_organization_id"),
        Index("ix_gmail_connections_organization_id", "organization_id"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    refresh_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    watch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_history_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
