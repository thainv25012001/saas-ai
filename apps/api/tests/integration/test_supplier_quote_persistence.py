from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.tenancy import tenant_session
from app.db.models.gmail_connection import GmailConnection
from app.db.models.supplier import Supplier, SupplierMapping
from app.db.models.supplier_quote import SupplierMessage, SupplierQuote, SupplierReport

pytestmark = pytest.mark.anyio


async def _create_quote_graph(tenant) -> tuple[GmailConnection, Supplier, SupplierMessage]:
    async with tenant_session(tenant) as session:
        connection = GmailConnection(
            organization_id=tenant.organization_id,
            email=f"quotes-{tenant.organization_id}@example.com",
            refresh_token_ciphertext="ciphertext:v1:test",
            status="connected",
        )
        supplier = Supplier(
            organization_id=tenant.organization_id,
            name="Supplier A",
            sender_emails=["quotes@supplier.example"],
            sender_domains=["supplier.example"],
            is_active=True,
        )
        session.add_all([connection, supplier])
        await session.flush()
        mapping = SupplierMapping(
            organization_id=tenant.organization_id,
            supplier_id=supplier.id,
            version=1,
            worksheet_name="Offers",
            header_row=1,
            column_mapping={"unit_price": "Price"},
            default_currency="USD",
            default_purchase_unit="piece",
            is_confirmed=True,
        )
        message = SupplierMessage(
            organization_id=tenant.organization_id,
            gmail_connection_id=connection.id,
            supplier_id=supplier.id,
            gmail_message_id=f"message-{tenant.organization_id}",
            sender_email="quotes@supplier.example",
            received_at=datetime.now(UTC),
            status="processed",
        )
        session.add_all([mapping, message])
        await session.flush()
        quote = SupplierQuote(
            organization_id=tenant.organization_id,
            supplier_id=supplier.id,
            supplier_message_id=message.id,
            mapping_id=mapping.id,
            attachment_id="attachment-1",
            worksheet_name="Offers",
            source_row_number=2,
            supplier_sku="SUP-1",
            product_name="Widget",
            unit_price=Decimal("12.00"),
            currency="USD",
            purchase_unit="piece",
            catalog_sale_price=None,
            profit_status="missing_sale_price",
            ranking_basis="purchase_price",
        )
        report = SupplierReport(
            organization_id=tenant.organization_id,
            supplier_message_id=message.id,
            profit_status="missing_sale_price",
            best_profit=None,
            summary_json={"missing_sale_price_count": 1},
        )
        session.add_all([quote, report])
        await session.flush()
        return connection, supplier, message


async def test_rls_hides_all_supplier_quote_entities_from_another_tenant(tenant_a, tenant_b):
    await _create_quote_graph(tenant_a)

    async with tenant_session(tenant_b) as session:
        for model in (
            GmailConnection,
            Supplier,
            SupplierMapping,
            SupplierMessage,
            SupplierQuote,
            SupplierReport,
        ):
            result = await session.execute(select(model))
            assert result.scalars().all() == []


async def test_mailbox_resolver_returns_only_tenant_and_connection_ids(tenant_a, owner_connection):
    connection, _, _ = await _create_quote_graph(tenant_a)

    result = await owner_connection.execute(
        text("SELECT organization_id, connection_id FROM resolve_gmail_mailboxes(:email)"),
        {"email": connection.email},
    )
    rows = result.mappings().all()

    assert rows == [{"organization_id": tenant_a.organization_id, "connection_id": connection.id}]
    assert set(rows[0]) == {"organization_id", "connection_id"}

    unmatched = await owner_connection.execute(
        text("SELECT * FROM resolve_gmail_mailboxes(:email)"),
        {"email": "not-connected@example.com"},
    )
    assert unmatched.all() == []
