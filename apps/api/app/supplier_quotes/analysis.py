from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.supplier_quotes.schemas import CanonicalQuoteRow, QuantityTier

RANKING_BASIS = Literal["gross_profit", "purchase_price", "none", "grouped"]


class SupplierOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    quote_id: str | None = None
    supplier_id: str
    supplier_name: str
    unit_price: Decimal | None
    currency: str | None
    purchase_unit: str | None
    quantity_tiers: list[QuantityTier] = Field(default_factory=list)


class MatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["confirmed", "suggested", "unmatched"]
    product_id: str | None = None
    catalog_sku: str | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str


class RankedSupplierOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str
    quote_id: str | None = None
    supplier_name: str
    unit_price: Decimal | None
    profit_status: str
    unit_gross_profit: Decimal | None = None
    margin: Decimal | None = None
    rank: int | None = None
    comparability_reasons: list[str] = Field(default_factory=list)


class OfferRankingGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str | None
    purchase_unit: str | None
    profit_status: str
    ranking_basis: Literal["gross_profit", "purchase_price", "none"]
    comparability_reasons: list[str] = Field(default_factory=list)
    sale_price_source_timestamp: datetime | None = None
    offers: list[RankedSupplierOffer]


class OfferComparison(BaseModel):
    """One product's comparison, with separate rankings for each currency/unit."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    profit_status: str
    unit_gross_profit: Decimal | None = None
    margin: Decimal | None = None
    ranking_basis: RANKING_BASIS
    comparability_reasons: list[str] = Field(default_factory=list)
    sale_price_source_timestamp: datetime | None = None
    offer_groups: list[OfferRankingGroup]


def group_offers_by_product(
    offers: Sequence[SupplierOffer],
) -> dict[str, list[SupplierOffer]]:
    """Partition matched offers so prices for different products never share a rank."""
    grouped: dict[str, list[SupplierOffer]] = defaultdict(list)
    for offer in offers:
        grouped[offer.product_id].append(offer)
    return dict(sorted(grouped.items()))


def compare_product_offers(
    offers: Sequence[SupplierOffer],
    catalog_product: object,
    now: datetime,
) -> OfferComparison:
    """Rank offers only within one product and compatible currency/unit groups."""
    product_id = str(getattr(catalog_product, "id", ""))
    if not offers:
        return OfferComparison(
            product_id=product_id,
            profit_status="incomplete",
            ranking_basis="none",
            comparability_reasons=["no_supplier_offers"],
            sale_price_source_timestamp=getattr(catalog_product, "price_updated_at", None),
            offer_groups=[],
        )

    product_ids = {offer.product_id for offer in offers}
    if len(product_ids) != 1:
        raise ValueError("compare_product_offers accepts offers for one product only")
    only_product_id = next(iter(product_ids))
    if product_id and product_id != only_product_id:
        raise ValueError("supplier offers do not belong to the supplied catalogue product")
    product_id = only_product_id

    sale_price = _decimal_or_none(getattr(catalog_product, "price", None))
    sale_currency = _text_or_none(getattr(catalog_product, "currency", None))
    sale_unit = _catalog_sale_unit(catalog_product)
    price_timestamp = getattr(catalog_product, "price_updated_at", None)
    sale_status = _sale_price_status(sale_price, sale_currency, sale_unit, price_timestamp, now)

    grouped: dict[tuple[str | None, str | None], list[SupplierOffer]] = defaultdict(list)
    for offer in offers:
        grouped[(_normalize_currency(offer.currency), _normalize_unit(offer.purchase_unit))].append(
            offer
        )

    result_groups = [
        _rank_group(
            group_offers,
            currency=currency,
            purchase_unit=unit,
            sale_price=sale_price,
            sale_currency=sale_currency,
            sale_unit=sale_unit,
            sale_status=sale_status,
            price_timestamp=price_timestamp,
        )
        for (currency, unit), group_offers in sorted(
            grouped.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
        )
    ]
    known = [
        offer
        for group in result_groups
        for offer in group.offers
        if offer.unit_gross_profit is not None
    ]
    best_profit = (
        max(
            known,
            key=lambda offer: (
                offer.unit_gross_profit
                if offer.unit_gross_profit is not None
                else Decimal("-Infinity")
            ),
        )
        if known
        else None
    )
    reasons = list(
        dict.fromkeys(reason for group in result_groups for reason in group.comparability_reasons)
    )
    if len(result_groups) > 1:
        top_status = "multiple_comparison_groups"
        top_basis: RANKING_BASIS = "grouped"
        reasons.append("currency_or_unit_groups_are_ranked_separately")
    elif result_groups:
        top_status = result_groups[0].profit_status
        top_basis = result_groups[0].ranking_basis
    else:
        top_status = "incomplete"
        top_basis = "none"

    return OfferComparison(
        product_id=product_id,
        profit_status=top_status,
        unit_gross_profit=best_profit.unit_gross_profit if best_profit else None,
        margin=best_profit.margin if best_profit else None,
        ranking_basis=top_basis,
        comparability_reasons=reasons,
        sale_price_source_timestamp=price_timestamp,
        offer_groups=result_groups,
    )


def match_catalog_product(
    quote: CanonicalQuoteRow,
    products: Sequence[object],
    confirmed_supplier_sku_map: Mapping[str, str],
) -> MatchResult:
    """Resolve confirmed SKU mappings and exact catalogue SKUs before suggestions.

    Mapping values may be a product UUID/id or the catalogue's external SKU.
    An exact product-name match is only a suggestion and must be reviewed.
    """
    sku = (quote.supplier_sku or "").strip()
    if sku and sku in confirmed_supplier_sku_map:
        target = confirmed_supplier_sku_map[sku]
        matches = [
            product
            for product in products
            if str(getattr(product, "id", "")) == target
            or str(getattr(product, "external_id", "")) == target
        ]
        if len(matches) == 1:
            product = matches[0]
            return _match_result(
                product, status="confirmed", confidence=1.0, reason="confirmed_supplier_sku_mapping"
            )
        return MatchResult(
            status="unmatched",
            confidence=0.0,
            reason="confirmed_supplier_sku_mapping_target_not_found_or_ambiguous",
        )

    if sku:
        matches = [product for product in products if getattr(product, "external_id", None) == sku]
        if len(matches) == 1:
            return _match_result(
                matches[0], status="confirmed", confidence=1.0, reason="exact_catalog_sku"
            )
        if len(matches) > 1:
            return MatchResult(status="unmatched", confidence=0.0, reason="duplicate_catalog_sku")

    name = _normalize_name(quote.product_name)
    if name:
        matches = [
            product
            for product in products
            if _normalize_name(getattr(product, "name", None)) == name
        ]
        if len(matches) == 1:
            return _match_result(
                matches[0],
                status="suggested",
                confidence=0.9,
                reason="exact_product_name_requires_review",
            )
        if len(matches) > 1:
            return MatchResult(status="unmatched", confidence=0.0, reason="ambiguous_product_name")

    return MatchResult(status="unmatched", confidence=0.0, reason="no_catalog_match")


def _rank_group(
    offers: Sequence[SupplierOffer],
    *,
    currency: str | None,
    purchase_unit: str | None,
    sale_price: Decimal | None,
    sale_currency: str | None,
    sale_unit: str | None,
    sale_status: str,
    price_timestamp: datetime | None,
) -> OfferRankingGroup:
    reasons: list[str] = []
    if any(offer.quantity_tiers for offer in offers):
        status = "quantity_tier_requires_order_quantity"
        reasons.append("quantity_tier_requires_common_order_quantity")
        ranked = [
            _ranked_offer(offer, status=status, rank=None, reason=status)
            for offer in sorted(offers, key=_offer_stable_key)
        ]
        return OfferRankingGroup(
            currency=currency,
            purchase_unit=purchase_unit,
            profit_status="incomplete",
            ranking_basis="none",
            comparability_reasons=reasons,
            sale_price_source_timestamp=price_timestamp,
            offers=ranked,
        )

    sale_usable = sale_status == "available"
    if not sale_usable:
        reasons.append(sale_status)
    if currency is None:
        reasons.append("missing_quote_currency")
    if purchase_unit is None:
        reasons.append("missing_purchase_unit")
    currency_matches = (
        currency is not None and sale_currency is not None and currency == sale_currency.upper()
    )
    unit_matches = (
        purchase_unit is not None
        and sale_unit is not None
        and purchase_unit == _normalize_unit(sale_unit)
    )
    if sale_usable and not currency_matches:
        reasons.append("currency_mismatch")
    if sale_usable and not unit_matches:
        reasons.append("unit_mismatch" if sale_unit is not None else "missing_catalog_unit")
    can_calculate_profit = sale_usable and currency_matches and unit_matches
    status = "available" if can_calculate_profit else sale_status
    if sale_usable and not currency_matches:
        status = "currency_mismatch"
    elif sale_usable and not unit_matches:
        status = "unit_mismatch" if sale_unit is not None else "missing_catalog_unit"

    scored: list[tuple[SupplierOffer, str, Decimal | None, Decimal | None, list[str]]] = []
    for offer in offers:
        offer_reasons: list[str] = []
        price = offer.unit_price
        if price is None:
            offer_reasons.append("missing_quote_price")
            scored.append((offer, "missing_quote_price", None, None, offer_reasons))
            continue
        if not price.is_finite() or price <= 0:
            offer_reasons.append("invalid_quote_price")
            scored.append((offer, "invalid_quote_price", None, None, offer_reasons))
            continue
        if can_calculate_profit and sale_price is not None:
            profit = sale_price - price
            margin = (profit / sale_price * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            scored.append((offer, "available", profit, margin, offer_reasons))
        else:
            scored.append((offer, status, None, None, list(dict.fromkeys(reasons))))

    valid = [
        entry for entry in scored if entry[1] not in {"missing_quote_price", "invalid_quote_price"}
    ]
    if can_calculate_profit and valid:
        ranking_basis: Literal["gross_profit", "purchase_price", "none"] = "gross_profit"
        # Higher gross profit ranks first; for one product this is the same as
        # lower purchase price, but keeping the calculation explicit avoids
        # silently changing the metric if cost adjustments are added later.
        ordered = sorted(
            valid,
            key=lambda entry: (
                -(entry[2] if entry[2] is not None else Decimal("-Infinity")),
                _offer_stable_key(entry[0]),
            ),
        )
    elif valid:
        ranking_basis = "purchase_price"
        ordered = sorted(
            valid, key=lambda entry: (entry[0].unit_price, _offer_stable_key(entry[0]))
        )
    else:
        ranking_basis = "none"
        ordered = []

    rank_by_offer_id: dict[int, int] = {}
    previous_score: Decimal | None = None
    previous_rank = 0
    for position, entry in enumerate(ordered, start=1):
        score = entry[2] if ranking_basis == "gross_profit" else entry[0].unit_price
        if score != previous_score:
            previous_rank = position
            previous_score = score
        rank_by_offer_id[id(entry[0])] = previous_rank

    ranked = [
        _ranked_offer(
            offer,
            status=entry_status,
            rank=rank_by_offer_id.get(id(offer)),
            profit=profit,
            margin=margin,
            reasons=offer_reasons,
        )
        for offer, entry_status, profit, margin, offer_reasons in sorted(
            scored,
            key=lambda entry: (
                rank_by_offer_id.get(id(entry[0]), 10**9),
                _offer_stable_key(entry[0]),
            ),
        )
    ]
    if not valid:
        reasons.append("no_positive_quote_prices")
    group_status = "available" if can_calculate_profit and valid else status
    if not valid:
        group_status = "incomplete"

    return OfferRankingGroup(
        currency=currency,
        purchase_unit=purchase_unit,
        profit_status=group_status,
        ranking_basis=ranking_basis,
        comparability_reasons=list(dict.fromkeys(reasons)),
        sale_price_source_timestamp=price_timestamp,
        offers=ranked,
    )


def _ranked_offer(
    offer: SupplierOffer,
    *,
    status: str,
    rank: int | None,
    profit: Decimal | None = None,
    margin: Decimal | None = None,
    reason: str | None = None,
    reasons: list[str] | None = None,
) -> RankedSupplierOffer:
    resolved_reasons = list(reasons or [])
    if reason and reason not in resolved_reasons:
        resolved_reasons.append(reason)
    return RankedSupplierOffer(
        supplier_id=offer.supplier_id,
        quote_id=offer.quote_id,
        supplier_name=offer.supplier_name,
        unit_price=offer.unit_price,
        profit_status=status,
        unit_gross_profit=profit,
        margin=margin,
        rank=rank,
        comparability_reasons=resolved_reasons,
    )


def _sale_price_status(
    price: Decimal | None,
    currency: str | None,
    sale_unit: str | None,
    timestamp: datetime | None,
    now: datetime,
) -> str:
    if price is None:
        return "missing_sale_price"
    if not price.is_finite() or price <= 0:
        return "invalid_sale_price"
    if currency is None:
        return "missing_sale_price_currency"
    if sale_unit is None:
        return "missing_catalog_unit"
    if timestamp is None:
        return "stale_sale_price"
    observed_at = _as_utc(timestamp)
    current = _as_utc(now)
    age = current - observed_at
    freshness = timedelta(days=get_settings().gmail_sale_price_max_age_days)
    if age < timedelta(0) or age > freshness:
        return "stale_sale_price"
    return "available"


def _catalog_sale_unit(product: object) -> str | None:
    explicit = _text_or_none(getattr(product, "sale_unit", None))
    if explicit:
        return explicit
    attributes = getattr(product, "attributes", None)
    if isinstance(attributes, dict):
        return _text_or_none(attributes.get("sale_unit") or attributes.get("unit"))
    return None


def _match_result(
    product: object,
    *,
    status: Literal["confirmed", "suggested"],
    confidence: float,
    reason: str,
) -> MatchResult:
    return MatchResult(
        status=status,
        product_id=str(getattr(product, "id", getattr(product, "external_id", ""))),
        catalog_sku=_text_or_none(getattr(product, "external_id", None)),
        confidence=confidence,
        reason=reason,
    )


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalize_currency(value: object) -> str | None:
    text = _text_or_none(value)
    return text.upper() if text else None


def _normalize_unit(value: object) -> str | None:
    text = _text_or_none(value)
    return " ".join(text.casefold().split()) if text else None


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _offer_stable_key(offer: SupplierOffer) -> tuple[str, str]:
    return (offer.supplier_name.casefold(), offer.supplier_id)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
