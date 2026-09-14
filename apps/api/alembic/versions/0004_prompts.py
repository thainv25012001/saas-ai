"""prompts and prompt_versions, plus the deferred agents.prompt_id FK

Revision ID: 0004_prompts
Revises: 0003_agents
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0004_prompts"
down_revision = "0003_agents"
branch_labels = None
depends_on = None

_TIMESTAMPS = (
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
)


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("organization_id", "key", name="uq_prompt_org_key"),
    )
    op.create_index("ix_prompts_organization_id", "prompts", ["organization_id"])
    enable_rls(op, "prompts")

    op.create_table(
        "prompt_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prompt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_TIMESTAMPS,
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_version_number"),
    )
    op.create_index("ix_prompt_versions_organization_id", "prompt_versions", ["organization_id"])

    # At most one active version per prompt, enforced by the database rather
    # than by every call site remembering to deactivate the previous one.
    op.create_index(
        "uq_prompt_versions_one_active",
        "prompt_versions",
        ["prompt_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    enable_rls(op, "prompt_versions")

    op.create_foreign_key(
        "fk_agents_prompt_id",
        "agents",
        "prompts",
        ["prompt_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_prompt_id", "agents", type_="foreignkey")
    disable_rls(op, "prompt_versions")
    op.drop_index("uq_prompt_versions_one_active", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    disable_rls(op, "prompts")
    op.drop_table("prompts")
