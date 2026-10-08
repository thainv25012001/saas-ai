from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.db.models.gmail_connection import GmailConnection
from app.db.models.supplier_quote import SupplierMessage
from app.supplier_quotes.schemas import (
    CanonicalQuoteRow,
    SaveSupplierMappingInput,
    SupplierInput,
    WorkbookMapping,
    WorkbookRowIssue,
)
from app.supplier_quotes.service import SupplierQuoteService

pytestmark = pytest.mark.anyio


async def _setup(service: SupplierQuoteService) -> tuple[object, object]:
    supplier = await service.create_supplier(
        SupplierInput(
            name="Supplier A",
            sender_emails=["quotes@supplier.example"],
            sender_domains=["supplier.example"],
        )
    )
    connection = GmailConnection(
        organization_id=service.tenant.organization_id,
        email="quotes@example.com",
        refresh_token_ciphertext="ciphertext:v1:test",
        status="connected",
    )
    service.session.add(connection)
    await service.session.flush()
    message = SupplierMessage(
        organization_id=service.tenant.organization_id,
        gmail_connection_id=connection.id,
        supplier_id=supplier.id,
        gmail_message_id="gmail-message-1",
        sender_email="quotes@supplier.example",
        attachment_ids=[
            {
                "attachment_id": "attachment-1",
                "filename": "prices.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 2048,
            }
        ],
    )
    service.session.add(message)
    await service.session.flush()
    return supplier, message.id


def _mapping_input(message_id, *, confirmed: bool = False) -> SaveSupplierMappingInput:
    return SaveSupplierMappingInput(
        mapping=WorkbookMapping(
            worksheet_name="Offers",
            header_row=1,
            columns={"supplier_sku": "SKU", "product_name": "Name", "unit_price": "Price"},
            default_currency="USD",
            default_purchase_unit="piece",
        ),
        column_labels=["SKU", "Name", "Price", "Pack Size"],
        source_message_id=message_id,
        source_attachment_id="attachment-1",
        is_confirmed=confirmed,
    )


def _quote() -> CanonicalQuoteRow:
    return CanonicalQuoteRow(
        worksheet_name="Offers",
        source_row_number=2,
        supplier_sku="SUP-1",
        product_name="Widget",
        unit_price=Decimal("12.50"),
        currency="USD",
        purchase_unit="piece",
    )


async def test_supplier_crud_and_cross_tenant_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier = await service.create_supplier(
            SupplierInput(name="Supplier A", sender_emails=["quotes@supplier.example"])
        )
        assert [item.id for item in await service.list_suppliers()] == [supplier.id]
        updated = await service.update_supplier(
            supplier.id,
            SupplierInput(name="Supplier A Updated", sender_domains=["supplier.example"]),
        )
        assert updated.name == "Supplier A Updated"

    async with tenant_session(tenant_b) as session:
        service = SupplierQuoteService(session, tenant_b)
        assert await service.list_suppliers() == []
        with pytest.raises(NotFoundError):
            await service.update_supplier(
                supplier.id,
                SupplierInput(name="Cross tenant attempt"),
            )


async def test_mapping_versions_store_only_labels_and_proposed_mapping(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        first = await service.save_mapping(supplier.id, _mapping_input(message_id))
        second = await service.save_mapping(supplier.id, _mapping_input(message_id, confirmed=True))

    assert first.version == 1
    assert second.version == 2
    assert first.column_labels == ["SKU", "Name", "Price", "Pack Size"]
    assert first.source_message_id == message_id
    assert first.source_attachment_id == "attachment-1"
    assert first.is_confirmed is False
    assert second.is_confirmed is True
    assert "sample_rows" not in first.__dict__


async def test_quote_upsert_is_idempotent_and_report_is_retrievable(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        mapping = await service.save_mapping(supplier.id, _mapping_input(message_id))
        first = await service.upsert_quote_rows(
            supplier.id,
            message_id,
            "attachment-1",
            [_quote()],
            mapping_id=mapping.id,
        )
        second = await service.upsert_quote_rows(
            supplier.id,
            message_id,
            "attachment-1",
            [_quote()],
            mapping_id=mapping.id,
        )
        reports = await service.list_reports()
        report, quote_rows = await service.get_report(reports[0].id)

    assert len(first.quotes) == len(second.quotes) == 1
    assert first.quotes[0].id == second.quotes[0].id
    assert first.quotes[0].mapping_id == mapping.id
    assert report.id == reports[0].id
    assert [row.id for row in quote_rows] == [first.quotes[0].id]


async def test_a_large_price_list_is_stored_in_full(tenant_a):
    # One multi-row INSERT of 2,000 rows would exceed PostgreSQL's 32,767
    # bind-parameter limit; the parser itself allows 25,000 rows.
    rows = [
        _quote().model_copy(update={"source_row_number": number, "supplier_sku": f"SUP-{number}"})
        for number in range(2, 2002)
    ]
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        result = await service.upsert_quote_rows(supplier.id, message_id, "attachment-1", rows)

    assert len(result.quotes) == 2000


async def test_reprocessing_drops_rows_missing_from_the_new_parse(tenant_a):
    second_row = _quote().model_copy(update={"source_row_number": 3, "supplier_sku": "SUP-2"})
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        first = await service.upsert_quote_rows(
            supplier.id, message_id, "attachment-1", [_quote(), second_row]
        )
        second = await service.upsert_quote_rows(
            supplier.id, message_id, "attachment-1", [_quote()]
        )

    assert len(first.quotes) == 2
    assert [row.id for row in second.quotes] == [first.quotes[0].id]


async def test_failed_rows_are_recorded_without_discarding_valid_rows(tenant_a):
    issue = WorkbookRowIssue(
        worksheet_name="Offers",
        source_row_number=3,
        code="invalid_price",
        message="The unit price is missing or is not a positive decimal value.",
    )
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        mapping = await service.save_mapping(supplier.id, _mapping_input(message_id))
        batch = await service.upsert_quote_rows(
            supplier.id,
            message_id,
            "attachment-1",
            [_quote()],
            mapping_id=mapping.id,
            row_issues=[issue],
        )
        report, quote_rows = await service.get_report(batch.report.id)

    assert len(quote_rows) == 1
    assert quote_rows[0].unit_price == Decimal("12.50")
    assert report.summary_json["issue_count"] == 1
    assert report.summary_json["row_issues"][0]["code"] == "invalid_price"
    assert "call for price" not in str(report.summary_json)


async def test_only_the_owner_changes_supplier_configuration(tenant_a):
    import dataclasses

    from app.core.errors import PermissionDeniedError
    from app.db.models import MembershipRole

    async with tenant_session(tenant_a) as session:
        supplier, message_id = await _setup(SupplierQuoteService(session, tenant_a))
        proposal = await SupplierQuoteService(session, tenant_a).save_mapping(
            supplier.id, _mapping_input(message_id)
        )
        member = SupplierQuoteService(
            session, dataclasses.replace(tenant_a, role=MembershipRole.MEMBER)
        )
        # A member adding sender domain "gmail.com" would make every Gmail
        # sender a confirmed supplier: configuration is the owner's.
        with pytest.raises(PermissionDeniedError):
            await member.create_supplier(SupplierInput(name="Anyone"))
        with pytest.raises(PermissionDeniedError):
            await member.update_supplier(supplier.id, SupplierInput(name="Renamed"))
        with pytest.raises(PermissionDeniedError):
            await member.confirm_mapping(proposal.id, mapping=_mapping_input(message_id).mapping)


async def test_a_proposal_on_the_wrong_sheet_can_be_moved_to_another_candidate(tenant_a):
    from app.core.errors import ValidationError as AppValidationError

    options = [
        {"worksheet_name": "Cover", "header_row": 1, "labels": ["Supplier", "ACME"]},
        {"worksheet_name": "Quote", "header_row": 3, "labels": ["Item", "Price", "Cur", "UOM"]},
    ]
    async with tenant_session(tenant_a) as session:
        service = SupplierQuoteService(session, tenant_a)
        supplier, message_id = await _setup(service)
        proposal = await service.save_mapping_proposal(
            supplier.id,
            worksheet_name="Cover",
            header_row=1,
            column_mapping={},
            column_labels=["Supplier", "ACME"],
            source_message_id=message_id,
            source_attachment_id="attachment-1",
            sheet_options=options,
        )
        with pytest.raises(AppValidationError):
            await service.confirm_mapping(
                proposal.id,
                mapping=WorkbookMapping(
                    worksheet_name="Elsewhere",
                    header_row=2,
                    columns={"product_name": "Item", "unit_price": "Price"},
                    default_currency="USD",
                    default_purchase_unit="each",
                ),
            )
        confirmed = await service.confirm_mapping(
            proposal.id,
            mapping=WorkbookMapping(
                worksheet_name="Quote",
                header_row=3,
                columns={"product_name": "Item", "unit_price": "Price", "currency": "Cur"},
                default_purchase_unit="each",
            ),
        )

    assert (confirmed.worksheet_name, confirmed.header_row) == ("Quote", 3)
    assert confirmed.column_labels == ["Item", "Price", "Cur", "UOM"]
    assert confirmed.sheet_options == options
