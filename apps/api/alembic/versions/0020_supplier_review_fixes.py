"""supplier review fixes

Revision ID: 0020_supplier_review_fixes
Revises: 0019_gmail_maintenance_resolver

- `supplier_mappings.sheet_options`: every worksheet/header-row candidate of the
  proposal's workbook, with its header labels only, so an owner can correct a
  proposal that picked the wrong sheet or row.
- `suppliers.sku_aliases`: supplier SKU -> catalogue product id, remembered when an
  owner accepts a product match, so the next quote matches without review.
- `gmail_connections.refresh_token_ciphertext` nullable: disconnecting clears the
  credential but keeps the row, because deleting it cascaded away every supplier
  message, quote and report. A connected row must still hold a credential.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020_supplier_review_fixes"
down_revision = "0019_gmail_maintenance_resolver"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_mappings",
        sa.Column("sheet_options", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "suppliers",
        sa.Column("sku_aliases", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.alter_column("gmail_connections", "refresh_token_ciphertext", nullable=True)
    op.create_check_constraint(
        "ck_gmail_connections_connected_has_credential",
        "gmail_connections",
        "status = 'disconnected' OR refresh_token_ciphertext IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_gmail_connections_connected_has_credential",
        "gmail_connections",
        type_="check",
    )
    # A disconnected row has no credential to restore; the old schema could not
    # represent it (and deleted it on disconnect, with its history).
    op.execute("DELETE FROM gmail_connections WHERE refresh_token_ciphertext IS NULL")
    op.alter_column("gmail_connections", "refresh_token_ciphertext", nullable=False)
    op.drop_column("suppliers", "sku_aliases")
    op.drop_column("supplier_mappings", "sheet_options")
