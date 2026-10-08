from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.supplier_quotes.schemas import SaveSupplierMappingInput, SupplierInput, WorkbookMapping


def test_supplier_sender_rules_are_normalized_and_deduplicated() -> None:
    supplier = SupplierInput(
        name="  Supplier A  ",
        sender_emails=["Quotes@Supplier.example", "quotes@supplier.example"],
        sender_domains=["@Supplier.example", "supplier.example"],
    )

    assert supplier.name == "Supplier A"
    assert supplier.sender_emails == ["quotes@supplier.example"]
    assert supplier.sender_domains == ["supplier.example"]


def test_mapping_review_accepts_only_column_labels_and_mapping() -> None:
    proposal = SaveSupplierMappingInput(
        mapping=WorkbookMapping(
            worksheet_name="Offers",
            header_row=1,
            columns={"product_name": "Name", "unit_price": "Price"},
            default_currency="USD",
            default_purchase_unit="piece",
        ),
        column_labels=["Name", "Price", "Notes"],
        source_message_id=uuid.uuid4(),
        source_attachment_id="attachment-1",
    )

    assert proposal.column_labels == ["Name", "Price", "Notes"]
    with pytest.raises(ValidationError):
        SaveSupplierMappingInput.model_validate(
            {
                **proposal.model_dump(),
                "sample_rows": [["Widget", "12.50", "private cell"]],
            }
        )


def test_mapping_review_requires_both_saved_source_identifiers() -> None:
    with pytest.raises(ValidationError):
        SaveSupplierMappingInput(
            mapping=WorkbookMapping(
                worksheet_name="Offers",
                header_row=1,
                columns={"product_name": "Name", "unit_price": "Price"},
                default_currency="USD",
                default_purchase_unit="piece",
            ),
            column_labels=["Name", "Price"],
            source_attachment_id="attachment-1",
        )
