"""supplier_mapping_reviews

Revision ID: 0018_supplier_mapping_reviews
Revises: 0017_supplier_quotes
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_supplier_mapping_reviews"
down_revision = "0017_supplier_quotes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_mappings",
        sa.Column("column_labels", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "supplier_mappings",
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "supplier_mappings",
        sa.Column("source_attachment_id", sa.String(255), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_mappings_source_message_id_supplier_messages",
        "supplier_mappings",
        "supplier_messages",
        ["source_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_supplier_mappings_source_identifiers_together",
        "supplier_mappings",
        "(source_message_id IS NULL) = (source_attachment_id IS NULL)",
    )
    op.create_index(
        "ix_supplier_mappings_source_message_id",
        "supplier_mappings",
        ["source_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_mappings_source_message_id", table_name="supplier_mappings")
    op.drop_constraint(
        "ck_supplier_mappings_source_identifiers_together",
        "supplier_mappings",
        type_="check",
    )
    op.drop_constraint(
        "fk_supplier_mappings_source_message_id_supplier_messages",
        "supplier_mappings",
        type_="foreignkey",
    )
    op.drop_column("supplier_mappings", "source_attachment_id")
    op.drop_column("supplier_mappings", "source_message_id")
    op.drop_column("supplier_mappings", "column_labels")
