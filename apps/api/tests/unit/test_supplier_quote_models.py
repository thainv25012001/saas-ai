from __future__ import annotations

from sqlalchemy import UniqueConstraint

from app.db.models.gmail_connection import GmailConnection
from app.db.models.supplier import Supplier, SupplierMapping
from app.db.models.supplier_quote import SupplierMessage, SupplierQuote, SupplierReport


def _unique_column_sets(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_mailbox_message_mapping_and_quote_source_identities_are_unique() -> None:
    assert ("organization_id",) in _unique_column_sets(GmailConnection)
    assert ("gmail_connection_id", "gmail_message_id") in _unique_column_sets(SupplierMessage)
    assert ("supplier_id", "version") in _unique_column_sets(SupplierMapping)
    assert (
        "supplier_message_id",
        "attachment_id",
        "worksheet_name",
        "source_row_number",
    ) in _unique_column_sets(SupplierQuote)
    assert ("supplier_message_id",) in _unique_column_sets(SupplierReport)


def test_stored_sale_price_profit_and_report_projection_allow_missing_values() -> None:
    quote = SupplierQuote.__table__.c
    report = SupplierReport.__table__.c

    assert quote.catalog_sale_price.nullable is True
    assert quote.unit_gross_profit.nullable is True
    assert quote.margin_percent.nullable is True
    assert report.best_profit.nullable is True
    assert quote.profit_status.nullable is False


def test_gmail_connection_has_ciphertext_token_fields_only() -> None:
    columns = GmailConnection.__table__.c

    assert "refresh_token_ciphertext" in columns
    assert "refresh_token" not in columns
    assert "access_token" not in columns


def test_every_quote_entity_is_tenant_owned() -> None:
    for model in (
        GmailConnection,
        Supplier,
        SupplierMapping,
        SupplierMessage,
        SupplierQuote,
        SupplierReport,
    ):
        assert model.__table__.c.organization_id.nullable is False


def test_mapping_review_persists_header_labels_and_source_ids_without_sample_cells() -> None:
    columns = SupplierMapping.__table__.c

    assert columns.column_labels.nullable is False
    assert columns.source_message_id.nullable is True
    assert columns.source_attachment_id.nullable is True
    assert "sample_rows" not in columns
