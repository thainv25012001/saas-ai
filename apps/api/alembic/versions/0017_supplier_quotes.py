"""supplier_quotes

Revision ID: 0017_supplier_quotes
Revises: 0016_widget_settings
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls, runtime_role

revision = "0017_supplier_quotes"
down_revision = "0016_widget_settings"
branch_labels = None
depends_on = None

_RESOLVE_GMAIL_MAILBOXES_FUNCTION = """
CREATE FUNCTION resolve_gmail_mailboxes(p_email text)
RETURNS TABLE (organization_id uuid, connection_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT organization_id, id
    FROM gmail_connections
    WHERE email = lower(p_email) AND status = 'connected'
$$;
"""


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("price_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE products SET price_updated_at = updated_at WHERE price IS NOT NULL")

    op.create_table(
        "gmail_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=False),
        sa.Column("history_id", sa.String(128), nullable=True),
        sa.Column("watch_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_history_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="connected"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", name="uq_gmail_connections_organization_id"),
    )
    op.create_index(
        "ix_gmail_connections_organization_id", "gmail_connections", ["organization_id"]
    )

    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sender_emails", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("sender_domains", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_suppliers_organization_name"),
    )
    op.create_index("ix_suppliers_organization_id", "suppliers", ["organization_id"])

    op.create_table(
        "supplier_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("worksheet_name", sa.String(128), nullable=False),
        sa.Column("header_row", sa.Integer(), nullable=False),
        sa.Column("column_mapping", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("default_currency", sa.String(3), nullable=True),
        sa.Column("default_purchase_unit", sa.String(64), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("supplier_id", "version", name="uq_supplier_mappings_supplier_version"),
    )
    op.create_index(
        "ix_supplier_mappings_organization_id", "supplier_mappings", ["organization_id"]
    )
    op.create_index("ix_supplier_mappings_supplier_id", "supplier_mappings", ["supplier_id"])

    op.create_table(
        "supplier_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "gmail_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column("history_id", sa.String(128), nullable=True),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attachment_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("classification", sa.String(32), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "gmail_connection_id", "gmail_message_id", name="uq_supplier_messages_mailbox_message"
        ),
    )
    op.create_index(
        "ix_supplier_messages_organization_id", "supplier_messages", ["organization_id"]
    )
    op.create_index(
        "ix_supplier_messages_supplier_status",
        "supplier_messages",
        ["organization_id", "supplier_id", "status"],
    )

    op.create_table(
        "supplier_quotes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mapping_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_mappings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attachment_id", sa.String(255), nullable=False),
        sa.Column("worksheet_name", sa.String(128), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("supplier_sku", sa.String(255), nullable=True),
        sa.Column("product_name", sa.String(500), nullable=True),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("purchase_unit", sa.String(64), nullable=True),
        sa.Column("minimum_order_quantity", sa.Numeric(14, 4), nullable=True),
        sa.Column("quantity_tiers", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("lead_time", sa.String(255), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("match_status", sa.String(32), nullable=False, server_default="unmatched"),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("catalog_sale_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("catalog_sale_currency", sa.String(3), nullable=True),
        sa.Column("catalog_sale_unit", sa.String(64), nullable=True),
        sa.Column("catalog_sale_price_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profit_status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("unit_gross_profit", sa.Numeric(14, 4), nullable=True),
        sa.Column("margin_percent", sa.Numeric(7, 2), nullable=True),
        sa.Column("purchase_price_rank", sa.Integer(), nullable=True),
        sa.Column("ranking_basis", sa.String(32), nullable=False, server_default="none"),
        sa.Column("comparability_reasons", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "supplier_message_id",
            "attachment_id",
            "worksheet_name",
            "source_row_number",
            name="uq_supplier_quotes_source_row",
        ),
    )
    op.create_index("ix_supplier_quotes_organization_id", "supplier_quotes", ["organization_id"])
    op.create_index(
        "ix_supplier_quotes_supplier_product",
        "supplier_quotes",
        ["organization_id", "supplier_id", "product_id"],
    )
    op.create_index(
        "ix_supplier_quotes_source_message",
        "supplier_quotes",
        ["organization_id", "supplier_message_id"],
    )

    op.create_table(
        "supplier_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("profit_status", sa.String(40), nullable=False),
        sa.Column("best_profit", sa.Numeric(14, 4), nullable=True),
        sa.Column("best_margin_percent", sa.Numeric(7, 2), nullable=True),
        sa.Column("summary_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("supplier_message_id", name="uq_supplier_reports_message"),
    )
    op.create_index("ix_supplier_reports_organization_id", "supplier_reports", ["organization_id"])
    op.create_index(
        "ix_supplier_reports_created_at", "supplier_reports", ["organization_id", "created_at"]
    )

    for table in (
        "gmail_connections",
        "suppliers",
        "supplier_mappings",
        "supplier_messages",
        "supplier_quotes",
        "supplier_reports",
    ):
        enable_rls(op, table)

    op.execute(_RESOLVE_GMAIL_MAILBOXES_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_gmail_mailboxes(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_gmail_mailboxes(text) TO {runtime_role()}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_gmail_mailboxes(text)")
    for table in (
        "supplier_reports",
        "supplier_quotes",
        "supplier_messages",
        "supplier_mappings",
        "suppliers",
        "gmail_connections",
    ):
        disable_rls(op, table)
        op.drop_table(table)
    op.drop_column("products", "price_updated_at")
