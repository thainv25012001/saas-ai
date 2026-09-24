import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """An MCP credential bound to exactly one agent (docs/PHASE-7.md §2):
    the tools it can list and call are exactly that agent's granted tools,
    and its `organization_id`/`agent_id` are what `ToolContext` is built
    from -- never anything the request itself supplies.

    `key_hash` is the only form of the token this table ever stores -- see
    `app.api_keys.tokens`. `resolve_api_key`, the `SECURITY DEFINER`
    function this migration also creates, is the one path that may read
    this table without a tenant already established (authentication is
    what establishes it); every other read and write here goes through
    `ApiKeyService` under an ordinary `tenant_session`.
    """

    __tablename__ = "api_keys"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Shown in the dashboard: "sa_mcp_3f9a1c2e…". Never enough to
    # reconstruct the token -- see DISPLAY_PREFIX_LENGTH.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
