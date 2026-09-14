import enum
import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class Agent(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_agent_org_slug"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(
            AgentStatus,
            name="agent_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=AgentStatus.DRAFT,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="gpt-4o-mini")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    # No ForeignKey here (nor in the migration): `prompts` does not exist as
    # a table -- in the database or in Base.metadata -- until Task 8 adds
    # it. A ForeignKey("prompts.id") on an unmapped table blows up at flush
    # time (NoReferencedTableError from the unit of work's table sort), not
    # just at migration time, so this stays a plain nullable UUID until
    # Task 8 creates the target and can add the constraint on both sides.
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=True,
    )
    public_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class AgentConfig(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Behaviour, separated from identity: the playground edits this row
    constantly while the agent row stays stable."""

    __tablename__ = "agent_configs"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str] = mapped_column(String(50), nullable=False, default="friendly")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="I don't have that information. Would you like me to connect you "
        "with someone who does?",
    )
    enabled_tool_names: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retrieval_min_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_agent_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
