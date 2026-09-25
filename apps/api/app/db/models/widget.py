import uuid
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class WidgetPosition(StrEnum):
    RIGHT = "right"
    LEFT = "left"


class WidgetSettings(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Per-agent widget configuration (docs/superpowers/specs/
    2026-09-25-embeddable-widget-design.md §3): whether the chat widget is
    enabled, which domains may frame it, and its appearance and daily
    message cap.

    A missing row means "defaults, disabled" -- `WidgetSettingsService.get`
    returns `WidgetView`'s Python-side defaults rather than requiring one to
    exist. The row is created lazily, on the first `update`, not at agent
    creation, so every agent that existed before this migration needs no
    backfill.

    `agent_id` is `UNIQUE`: one settings row per agent, never per
    organization -- an organization with several agents gives each its own
    widget. `ON DELETE CASCADE` follows every other per-agent table in this
    schema (`api_keys`, `agent_configs`): deleting the agent takes its widget
    settings with it, not the other way around.

    `allowed_origins` stores exactly what later becomes a `frame-ancestors`
    source list -- see `app.widget.origins.normalize_origin` for the
    canonical form every element is reduced to before it ever reaches this
    column. Nothing here re-validates that shape; the service layer is the
    only writer.
    """

    __tablename__ = "widget_settings"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_widget_settings_agent_id"),
        CheckConstraint(
            "daily_message_cap BETWEEN 1 AND 100000",
            name="ck_widget_settings_daily_message_cap",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_origins: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    brand_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#2563eb")
    position: Mapped[WidgetPosition] = mapped_column(
        SAEnum(
            WidgetPosition,
            name="widget_position",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=WidgetPosition.RIGHT,
    )
    title: Mapped[str | None] = mapped_column(String(60), nullable=True)
    daily_message_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
