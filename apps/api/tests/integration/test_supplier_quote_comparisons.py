"""Which offers a comparison ranks, and what a report's headline profit means."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext, tenant_session
from app.db.models import GmailConnection, Product, SupplierQuote, SupplierReport
from app.supplier_quotes.comparisons import recalculate_product_comparisons
from app.supplier_quotes.schemas import CanonicalQuoteRow, SupplierInput
from app.supplier_quotes.service import SupplierQuoteService

pytestmark = pytest.mark.anyio

_NOW = datetime.now(UTC)


async def _product(
    session: AsyncSession, tenant: TenantContext, sku: str, currency: str
) -> Product:
    product = Product(
        organization_id=tenant.organization_id,
        external_id=sku,
        name=f"Product {sku}",
        slug=sku.lower(),
        price=Decimal("15.00"),
        currency=currency,
        price_updated_at=_NOW,
        attributes={"sale_unit": "each"},
    )
    session.add(product)
    await session.flush()
    return product


async def _offer(
    service: SupplierQuoteService,
    connection: GmailConnection,
    *,
    supplier_id: uuid.UUID,
    gmail_id: str,
    received_at: datetime,
    product: Product,
    price: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    message, _ = await service.get_or_create_message(
        connection_id=connection.id,
        gmail_message_id=gmail_id,
        history_id="1",
        sender_email="quotes@example.com",
        received_at=received_at,
        attachment_ids=[{"attachment_id": "part:1"}],
    )
    result = await service.upsert_quote_rows(
        supplier_id,
        message.id,
        "part:1",
        [
            CanonicalQuoteRow(
                worksheet_name="Offers",
                source_row_number=2,
                supplier_sku=product.external_id,
                product_name=product.name,
                unit_price=Decimal(price),
                currency=product.currency,
                purchase_unit="each",
            )
        ],
    )
    [quote] = result.quotes
    quote.product_id = product.id
    quote.match_status = "confirmed"
    await service.session.flush()
    return message.id, quote.id


async def _setup(session: AsyncSession, tenant: TenantContext):
    service = SupplierQuoteService(session, tenant)
    connection = GmailConnection(
        organization_id=tenant.organization_id,
        email="buyer@example.com",
        refresh_token_ciphertext="test-only-ciphertext",
        history_id="1",
    )
    session.add(connection)
    await session.flush()
    x = await service.create_supplier(SupplierInput(name="Supplier X"))
    y = await service.create_supplier(SupplierInput(name="Supplier Y"))
    return service, connection, x, y


async def test_a_suppliers_newer_quote_replaces_its_older_one_in_the_ranking(
    tenant_a: TenantContext,
) -> None:
    async with tenant_session(tenant_a) as session:
        service, connection, x, y = await _setup(session, tenant_a)
        product = await _product(session, tenant_a, "P-1", "USD")
        _old_msg, old_x = await _offer(
            service,
            connection,
            supplier_id=x.id,
            gmail_id="x-old",
            received_at=_NOW - timedelta(days=30),
            product=product,
            price="10.00",
        )
        new_msg, new_x = await _offer(
            service,
            connection,
            supplier_id=x.id,
            gmail_id="x-new",
            received_at=_NOW,
            product=product,
            price="12.00",
        )
        _y_msg, y_quote = await _offer(
            service,
            connection,
            supplier_id=y.id,
            gmail_id="y",
            received_at=_NOW - timedelta(days=1),
            product=product,
            price="11.00",
        )

        await recalculate_product_comparisons(session, tenant_a, new_msg, {product.id})

        quotes = {
            quote_id: await session.get(SupplierQuote, quote_id)
            for quote_id in (old_x, new_x, y_quote)
        }

    assert all(quote is not None for quote in quotes.values())
    assert quotes[old_x].purchase_price_rank is None
    assert quotes[old_x].profit_status == "superseded"
    assert quotes[y_quote].purchase_price_rank == 1
    assert quotes[new_x].purchase_price_rank == 2


async def test_a_reports_best_profit_comes_from_its_own_email_in_one_currency(
    tenant_a: TenantContext,
) -> None:
    async with tenant_session(tenant_a) as session:
        service, connection, x, y = await _setup(session, tenant_a)
        product = await _product(session, tenant_a, "P-1", "USD")
        this_msg, _ = await _offer(
            service,
            connection,
            supplier_id=x.id,
            gmail_id="this",
            received_at=_NOW,
            product=product,
            price="12.00",
        )
        # Another supplier's cheaper offer for the same product, from another
        # email: it ranks above, but it is not this email's profit.
        await _offer(
            service,
            connection,
            supplier_id=y.id,
            gmail_id="other",
            received_at=_NOW,
            product=product,
            price="11.00",
        )

        await recalculate_product_comparisons(session, tenant_a, this_msg, {product.id})
        report = await session.scalar(
            select(SupplierReport).where(SupplierReport.supplier_message_id == this_msg)
        )

    assert report is not None
    assert report.best_profit == Decimal("3.0000")
    assert report.summary_json["best_profit_currency"] == "USD"


async def test_profits_in_different_currencies_have_no_single_best(
    tenant_a: TenantContext,
) -> None:
    async with tenant_session(tenant_a) as session:
        service, connection, x, _y = await _setup(session, tenant_a)
        usd = await _product(session, tenant_a, "P-USD", "USD")
        vnd = await _product(session, tenant_a, "P-VND", "VND")
        message, _ = await service.get_or_create_message(
            connection_id=connection.id,
            gmail_message_id="mixed",
            history_id="1",
            sender_email="quotes@example.com",
            received_at=_NOW,
            attachment_ids=[{"attachment_id": "part:1"}],
        )
        result = await service.upsert_quote_rows(
            x.id,
            message.id,
            "part:1",
            [
                CanonicalQuoteRow(
                    worksheet_name="Offers",
                    source_row_number=row,
                    supplier_sku=p.external_id,
                    product_name=p.name,
                    unit_price=Decimal("10.00"),
                    currency=p.currency,
                    purchase_unit="each",
                )
                for row, p in ((2, usd), (3, vnd))
            ],
        )
        for quote, product in zip(result.quotes, (usd, vnd), strict=True):
            quote.product_id = product.id
            quote.match_status = "confirmed"
        await session.flush()

        await recalculate_product_comparisons(session, tenant_a, message.id, {usd.id, vnd.id})
        report = await session.scalar(
            select(SupplierReport).where(SupplierReport.supplier_message_id == message.id)
        )

    assert report is not None
    assert report.best_profit is None
    assert report.summary_json.get("best_profit_currency") is None
    assert report.profit_status == "available"
