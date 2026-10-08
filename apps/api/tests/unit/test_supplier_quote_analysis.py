from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.supplier_quotes.analysis import (
    SupplierOffer,
    compare_product_offers,
    group_offers_by_product,
    match_catalog_product,
)
from app.supplier_quotes.schemas import CanonicalQuoteRow, QuantityTier

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _product(
    *,
    price: Decimal | None = Decimal("20"),
    currency: str | None = "USD",
    sale_unit: str | None = "piece",
    price_updated_at: datetime | None = NOW - timedelta(days=5),
    external_id: str = "SKU-1",
    product_id: str = "product-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=product_id,
        external_id=external_id,
        name=f"Product {external_id}",
        price=price,
        currency=currency,
        sale_unit=sale_unit,
        price_updated_at=price_updated_at,
        attributes={},
    )


def _offer(
    supplier_id: str,
    price: Decimal | None,
    *,
    product_id: str = "product-1",
    currency: str = "USD",
    unit: str = "piece",
    quantity_tiers: list[QuantityTier] | None = None,
) -> SupplierOffer:
    return SupplierOffer(
        product_id=product_id,
        supplier_id=supplier_id,
        supplier_name=supplier_id.title(),
        unit_price=price,
        currency=currency,
        purchase_unit=unit,
        quantity_tiers=quantity_tiers or [],
    )


def _quote(*, sku: str | None = "SKU-1", name: str | None = "Product SKU-1") -> CanonicalQuoteRow:
    return CanonicalQuoteRow(
        worksheet_name="Offers",
        source_row_number=2,
        supplier_sku=sku,
        product_name=name,
        unit_price=Decimal("10"),
        currency="USD",
        purchase_unit="piece",
    )


def test_known_fresh_sale_price_computes_gross_profit_and_margin() -> None:
    comparison = compare_product_offers(
        [_offer("supplier-a", Decimal("12"))],
        _product(),
        now=NOW,
    )

    assert comparison.profit_status == "available"
    assert comparison.unit_gross_profit == Decimal("8")
    assert comparison.margin == Decimal("40.00")
    assert comparison.ranking_basis == "gross_profit"
    assert comparison.sale_price_source_timestamp == NOW - timedelta(days=5)
    assert comparison.offer_groups[0].offers[0].supplier_id == "supplier-a"


def test_missing_sale_price_keeps_profit_unknown_and_ranks_by_purchase_price() -> None:
    comparison = compare_product_offers(
        [_offer("expensive", Decimal("15")), _offer("cheap", Decimal("9"))],
        _product(price=None, price_updated_at=None),
        now=NOW,
    )

    assert comparison.profit_status == "missing_sale_price"
    assert comparison.unit_gross_profit is None
    assert comparison.margin is None
    assert comparison.ranking_basis == "purchase_price"
    assert [offer.supplier_id for offer in comparison.offer_groups[0].offers] == [
        "cheap",
        "expensive",
    ]
    assert [offer.rank for offer in comparison.offer_groups[0].offers] == [1, 2]


def test_missing_quote_price_is_not_ranked_or_used_for_profit() -> None:
    comparison = compare_product_offers(
        [_offer("supplier-a", None), _offer("supplier-b", Decimal("12"))],
        _product(),
        now=NOW,
    )

    ranked = comparison.offer_groups[0].offers
    assert comparison.profit_status == "available"
    assert [(offer.supplier_id, offer.rank) for offer in ranked] == [
        ("supplier-b", 1),
        ("supplier-a", None),
    ]
    assert ranked[-1].profit_status == "missing_quote_price"
    assert ranked[-1].unit_gross_profit is None


def test_currency_mismatch_does_not_calculate_profit() -> None:
    comparison = compare_product_offers(
        [_offer("supplier-a", Decimal("12"), currency="EUR")],
        _product(),
        now=NOW,
    )

    offer = comparison.offer_groups[0].offers[0]
    assert offer.profit_status == "currency_mismatch"
    assert offer.unit_gross_profit is None
    assert offer.rank == 1
    assert comparison.ranking_basis == "purchase_price"
    assert "currency_mismatch" in offer.comparability_reasons


def test_unit_mismatch_does_not_calculate_profit() -> None:
    comparison = compare_product_offers(
        [_offer("supplier-a", Decimal("12"), unit="box")],
        _product(),
        now=NOW,
    )

    offer = comparison.offer_groups[0].offers[0]
    assert offer.profit_status == "unit_mismatch"
    assert offer.unit_gross_profit is None
    assert offer.rank == 1


def test_offers_are_grouped_by_product_before_any_ranking() -> None:
    offers = [
        _offer("supplier-a", Decimal("8"), product_id="product-1"),
        _offer("supplier-b", Decimal("4"), product_id="product-2"),
        _offer("supplier-c", Decimal("6"), product_id="product-1"),
    ]

    grouped = group_offers_by_product(offers)

    assert set(grouped) == {"product-1", "product-2"}
    assert [offer.supplier_id for offer in grouped["product-1"]] == [
        "supplier-a",
        "supplier-c",
    ]
    first = compare_product_offers(grouped["product-1"], _product(), now=NOW)
    second = compare_product_offers(grouped["product-2"], _product(product_id="product-2"), now=NOW)
    assert first.product_id == "product-1"
    assert second.product_id == "product-2"
    assert {offer.supplier_id for offer in first.offer_groups[0].offers} == {
        "supplier-a",
        "supplier-c",
    }
    assert [offer.supplier_id for offer in first.offer_groups[0].offers] == [
        "supplier-c",
        "supplier-a",
    ]


def test_invalid_prices_are_excluded_and_reported() -> None:
    comparison = compare_product_offers(
        [_offer("zero", Decimal("0")), _offer("negative", Decimal("-1"))],
        _product(),
        now=NOW,
    )

    assert comparison.profit_status == "incomplete"
    assert comparison.ranking_basis == "none"
    assert all(offer.rank is None for offer in comparison.offer_groups[0].offers)
    assert {offer.profit_status for offer in comparison.offer_groups[0].offers} == {
        "invalid_quote_price"
    }


def test_equal_purchase_prices_receive_the_same_rank() -> None:
    comparison = compare_product_offers(
        [_offer("supplier-b", Decimal("10")), _offer("supplier-a", Decimal("10"))],
        _product(price=None, price_updated_at=None),
        now=NOW,
    )

    assert [(offer.supplier_id, offer.rank) for offer in comparison.offer_groups[0].offers] == [
        ("supplier-a", 1),
        ("supplier-b", 1),
    ]


def test_absent_or_stale_catalog_price_never_produces_profit() -> None:
    missing = compare_product_offers(
        [_offer("supplier-a", Decimal("12"))],
        _product(price=None, price_updated_at=None),
        now=NOW,
    )
    stale = compare_product_offers(
        [_offer("supplier-a", Decimal("12"))],
        _product(price_updated_at=NOW - timedelta(days=91)),
        now=NOW,
    )

    assert missing.unit_gross_profit is None
    assert missing.profit_status == "missing_sale_price"
    assert stale.unit_gross_profit is None
    assert stale.profit_status == "stale_sale_price"
    assert stale.sale_price_source_timestamp == NOW - timedelta(days=91)


def test_quantity_tiers_need_a_common_order_quantity_before_ranking() -> None:
    tiered = _offer(
        "tiered",
        Decimal("12"),
        quantity_tiers=[QuantityTier(minimum_quantity=Decimal("10"), unit_price=Decimal("10"))],
    )
    comparison = compare_product_offers(
        [tiered, _offer("flat", Decimal("11"))], _product(), now=NOW
    )

    group = comparison.offer_groups[0]
    assert comparison.profit_status == "incomplete"
    assert comparison.ranking_basis == "none"
    assert all(offer.rank is None for offer in group.offers)
    assert {offer.profit_status for offer in group.offers} == {
        "quantity_tier_requires_order_quantity"
    }


def test_confirmed_supplier_sku_mapping_and_exact_catalog_sku_precede_suggestions() -> None:
    product = _product()
    confirmed = match_catalog_product(_quote(sku="ALT-1"), [product], {"ALT-1": "product-1"})
    exact = match_catalog_product(_quote(sku="SKU-1"), [product], {})
    suggested = match_catalog_product(_quote(sku=None), [product], {})

    assert confirmed.status == "confirmed"
    assert confirmed.product_id == "product-1"
    assert exact.status == "confirmed"
    assert exact.product_id == "product-1"
    assert suggested.status == "suggested"
    assert suggested.product_id == "product-1"
