import enum
import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ToolType(enum.StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    HTTP = "http"


class Tool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registry row for something an agent can call. The behaviour lives in
    code (`app/tools/builtin/`) or behind an external endpoint described by
    `config`; this row is only what makes it discoverable and toggleable.

    `organization_id` is deliberately nullable and this table deliberately
    does not use `TenantMixin`: NULL means a global builtin every
    organization can see (`retrieve_knowledge`, `search_products`, ...),
    seeded once rather than duplicated per tenant. That also means this is
    the one tenant-owned table in the schema that cannot use `enable_rls` --
    its policy compares `organization_id` for equality only, and a NULL
    compared to anything is UNKNOWN rather than TRUE, which would hide every
    builtin from every organization. See the hand-written policy in
    `alembic/versions/0008_tools_and_leads.py` for the exception and why it
    is not a general change to `enable_rls`.
    """

    __tablename__ = "tools"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[ToolType] = mapped_column(
        SAEnum(ToolType, name="tool_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AgentToolLink(TenantMixin, TimestampMixin, Base):
    """Which tools an agent may call, and per-agent overrides (e.g. a
    narrower `top_k`, a different confirmation threshold).

    Composite primary key rather than a surrogate id: the row's whole
    identity is "this agent, this tool", so the key states that directly
    instead of needing a separate unique constraint to say the same thing.

    `organization_id` duplicates what is reachable via
    `agent_id -> agents.organization_id`, but every tenant-owned table in
    this schema gets RLS off its own column (see `TenantMixin`'s docstring),
    not off a join, and `docs/ARCHITECTURE.md` §2.3's two-layer predicate
    for this table is exactly that: the plain single-org `enable_rls` policy
    applies here even though `tool_id` may point at a NULL-org builtin row --
    an agent's own tool *selection* is never itself global.
    """

    __tablename__ = "agent_tools"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"), primary_key=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class MessageToolCall(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One tool invocation the model made mid-turn, and its outcome.

    Append-only, like `Message`/`MessageCitation`: written once by the
    orchestrator when a call resolves and never edited afterwards, so
    `updated_at` (from `TimestampMixin`) never advances past insert --
    carried anyway for consistency with every other table in this schema,
    per the identical note on `Message`.

    `tool_call_id` is the *provider's* id for this call (Anthropic's
    `tool_use_id`, OpenAI's `tool_call.id`) -- it is what ties this row back
    to the matching `tool_use`/`tool_result` content block in
    `Message.content_blocks`, not a foreign key to `tools.id`, because a
    provider issues it and this schema has no way to enforce it as one.
    `tool_name` is stored alongside it so this row still reads on its own
    after the tool that produced it is renamed or removed from the registry.
    """

    __tablename__ = "message_tool_calls"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
