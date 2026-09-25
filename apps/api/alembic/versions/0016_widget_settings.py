"""widget_settings

Revision ID: 0016_widget_settings
Revises: 0015_api_keys

Phase 8 Task 1 (docs/superpowers/specs/2026-09-25-embeddable-widget-design.md
§3): one row per agent describing whether its embeddable chat widget is
enabled and how it looks. `widget_settings` is tenant-owned like every other
table here (`enable_rls`), but the widget's own public-key lookup -- what a
loader script's `data-key` resolves to before any organization is known at
all -- must happen *before* that. So this migration also creates
`resolve_widget`, copying `resolve_api_key`'s shape
(`alembic/versions/0015_api_keys.py`): a `SECURITY DEFINER` function owned by
the migrating role (and so not itself subject to `agents`' un-FORCEd RLS
policy), `EXECUTE` revoked from `PUBLIC` and granted only to `app_user`, and
`search_path` pinned so a session-level `search_path` change cannot redirect
it. It reads only `agents` (never this table) and returns exactly the two
ids `app.widget.service.resolve_public_key` needs to build a `TenantContext`
and take it from there under an ordinary `tenant_session`, same as
`resolve_api_key`.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0016_widget_settings"
down_revision = "0015_api_keys"
branch_labels = None
depends_on = None

_RESOLVE_WIDGET_FUNCTION = """
CREATE FUNCTION resolve_widget(p_public_key text)
RETURNS TABLE (organization_id uuid, agent_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$ SELECT organization_id, id FROM agents WHERE public_key = p_public_key $$;
"""


def upgrade() -> None:
    op.create_table(
        "widget_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "allowed_origins",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("brand_color", sa.String(7), nullable=False, server_default="#2563eb"),
        sa.Column(
            "position",
            sa.Enum("right", "left", name="widget_position"),
            nullable=False,
            server_default="right",
        ),
        sa.Column("title", sa.String(60), nullable=True),
        sa.Column("daily_message_cap", sa.Integer(), nullable=False, server_default="500"),
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
        sa.UniqueConstraint("agent_id", name="uq_widget_settings_agent_id"),
        sa.CheckConstraint(
            "daily_message_cap BETWEEN 1 AND 100000",
            name="ck_widget_settings_daily_message_cap",
        ),
    )
    op.create_index("ix_widget_settings_organization_id", "widget_settings", ["organization_id"])
    enable_rls(op, "widget_settings")

    op.execute(_RESOLVE_WIDGET_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_widget(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_widget(text) TO app_user")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_widget(text)")
    disable_rls(op, "widget_settings")
    op.drop_table("widget_settings")
    op.execute("DROP TYPE IF EXISTS widget_position")
