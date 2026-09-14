"""rls_probe: a minimal tenant-owned table used to test RLS itself

Revision ID: 0002_rls_probe
Revises: 0001_identity
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0002_rls_probe"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rls_probe",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255), nullable=False),
    )
    op.create_index("ix_rls_probe_org", "rls_probe", ["organization_id"])
    enable_rls(op, "rls_probe")


def downgrade() -> None:
    disable_rls(op, "rls_probe")
    op.drop_index("ix_rls_probe_org", table_name="rls_probe")
    op.drop_table("rls_probe")
