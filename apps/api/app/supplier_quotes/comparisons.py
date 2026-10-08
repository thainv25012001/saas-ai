"""Rank supplier offers per catalogue product and summarise them on a report.

Shared by the Gmail worker (after a message is parsed) and by match
confirmation (after an owner accepts a product match), so both recompute
comparisons the same way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.db.models import Product, Supplier, SupplierMessage, SupplierQuote, SupplierReport
from app.supplier_quotes.analysis import (
    SupplierOffer,
    compare_product_offers,
    group_offers_by_product,
)
from app.supplier_quotes.schemas import QuantityTier


def product_sale_unit(product: Product) -> str | None:
    attributes = product.attributes
    if not isinstance(attributes, dict):
        return None
    value = attributes.get("sale_unit") or attributes.get("unit")
    return value.strip() if isinstance(value, str) and value.strip() else None


async def recalculate_product_comparisons(
    session: AsyncSession,
    tenant: TenantContext,
    message_id: uuid.UUID,
    product_ids: set[uuid.UUID],
) -> None:
    today = datetime.now(UTC).date()
    quote_result = await session.execute(
        select(SupplierQuote, Supplier, SupplierMessage.received_at, SupplierMessage.created_at)
        .join(
            Supplier,
            (Supplier.id == SupplierQuote.supplier_id)
            & (Supplier.organization_id == SupplierQuote.organization_id),
        )
        .join(
            SupplierMessage,
            (SupplierMessage.id == SupplierQuote.supplier_message_id)
            & (SupplierMessage.organization_id == SupplierQuote.organization_id),
        )
        .where(
            SupplierQuote.organization_id == tenant.organization_id,
            SupplierQuote.product_id.in_(product_ids),
            SupplierQuote.match_status == "confirmed",
            Supplier.is_active.is_(True),
            or_(SupplierQuote.valid_until.is_(None), SupplierQuote.valid_until >= today),
        )
    )
    quote_rows, superseded = _latest_offers(list(quote_result.all()))
    for quote in superseded:
        # A supplier's newer quote for the same product, currency and unit
        # replaces this one: ranking both would report an obsolete price as
        # the lowest on offer.
        quote.purchase_price_rank = None
        quote.ranking_basis = "none"
        quote.profit_status = "superseded"
        quote.unit_gross_profit = None
        quote.margin_percent = None
        quote.comparability_reasons = ["superseded_by_newer_quote"]
    products_result = await session.execute(
        select(Product).where(
            Product.organization_id == tenant.organization_id,
            Product.id.in_(product_ids),
        )
    )
    products_by_id = {product.id: product for product in products_result.scalars().all()}
    rows_by_id = {str(quote.id): quote for quote, _supplier in quote_rows}
    offers = [
        SupplierOffer(
            product_id=str(quote.product_id),
            quote_id=str(quote.id),
            supplier_id=str(supplier.id),
            supplier_name=supplier.name,
            unit_price=quote.unit_price,
            currency=quote.currency,
            purchase_unit=quote.purchase_unit,
            quantity_tiers=[QuantityTier.model_validate(tier) for tier in quote.quantity_tiers],
        )
        for quote, supplier in quote_rows
        if quote.product_id is not None
    ]
    comparisons: list[dict[str, Any]] = []
    offers_by_product = group_offers_by_product(offers)
    for product_id, product_offers in offers_by_product.items():
        product = products_by_id.get(uuid.UUID(product_id))
        if product is None:
            continue
        comparison = compare_product_offers(product_offers, product, datetime.now(UTC))
        groups: list[dict[str, Any]] = []
        for group in comparison.offer_groups:
            for ranked in group.offers:
                ranked_quote = rows_by_id.get(ranked.quote_id or "")
                if ranked_quote is None:
                    continue
                ranked_quote.catalog_sale_price = product.price
                ranked_quote.catalog_sale_currency = product.currency
                ranked_quote.catalog_sale_unit = product_sale_unit(product)
                ranked_quote.catalog_sale_price_updated_at = product.price_updated_at
                ranked_quote.profit_status = ranked.profit_status
                ranked_quote.unit_gross_profit = ranked.unit_gross_profit
                ranked_quote.margin_percent = ranked.margin
                ranked_quote.purchase_price_rank = ranked.rank
                ranked_quote.ranking_basis = group.ranking_basis
                ranked_quote.comparability_reasons = ranked.comparability_reasons
            groups.append(group.model_dump(mode="json"))
        comparisons.append(
            {
                "product_id": product_id,
                "product_name": product.name,
                "profit_status": comparison.profit_status,
                "ranking_basis": comparison.ranking_basis,
                "sale_price_source_timestamp": (
                    comparison.sale_price_source_timestamp.isoformat()
                    if comparison.sale_price_source_timestamp
                    else None
                ),
                "offer_groups": groups,
            }
        )

    report_result = await session.execute(
        select(SupplierReport).where(
            SupplierReport.organization_id == tenant.organization_id,
            SupplierReport.supplier_message_id == message_id,
        )
    )
    report = report_result.scalar_one_or_none()
    if report is None:
        return
    # The headline belongs to this email: other emails' offers are ranked
    # alongside it, but their profit is not this report's. And a maximum
    # across currencies compares numbers that are not comparable, so a
    # report spanning several has no single best figure.
    own = [quote for quote, _supplier in quote_rows if quote.supplier_message_id == message_id]
    profits_by_currency: dict[str, list[Any]] = {}
    for quote in own:
        if quote.unit_gross_profit is not None and quote.currency:
            profits_by_currency.setdefault(quote.currency, []).append(quote.unit_gross_profit)
    known_margins = [quote.margin_percent for quote in own if quote.margin_percent is not None]
    summary = dict(report.summary_json)
    summary["product_comparisons"] = comparisons
    summary.pop("best_profit_currency", None)
    if len(profits_by_currency) == 1:
        [(currency, profits)] = profits_by_currency.items()
        report.best_profit = max(profits)
        summary["best_profit_currency"] = currency
    else:
        report.best_profit = None
    report.summary_json = summary
    report.best_margin_percent = max(known_margins) if known_margins else None
    report.profit_status = (
        "available"
        if profits_by_currency
        else comparisons[0]["profit_status"]
        if comparisons
        else "review_required"
    )


def _latest_offers(
    rows: list[Any],
) -> tuple[list[tuple[SupplierQuote, Supplier]], list[SupplierQuote]]:
    """Split quotes into each supplier's latest offer and the ones it replaced.

    Keyed by supplier, product, currency and unit: a quote in another
    currency or unit is a different offer, not a replacement. "Latest" is the
    email's received time (its creation time when Gmail gave none); every
    row of that latest email stays current.
    """
    latest: dict[tuple[Any, ...], tuple[datetime, str]] = {}
    for quote, _supplier, received_at, created_at in rows:
        key = (quote.supplier_id, quote.product_id, quote.currency, quote.purchase_unit)
        stamp = (received_at or created_at, str(quote.supplier_message_id))
        if key not in latest or stamp > latest[key]:
            latest[key] = stamp
    current: list[tuple[SupplierQuote, Supplier]] = []
    superseded: list[SupplierQuote] = []
    for quote, supplier, _received_at, _created_at in rows:
        key = (quote.supplier_id, quote.product_id, quote.currency, quote.purchase_unit)
        if str(quote.supplier_message_id) == latest[key][1]:
            current.append((quote, supplier))
        else:
            superseded.append(quote)
    return current, superseded
