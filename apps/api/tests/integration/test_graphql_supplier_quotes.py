"""GraphQL setup, review, and quote-report contract for supplier offers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.tenancy import TenantContext, tenant_session
from app.db.models import GmailConnection, MembershipRole
from app.graphql.schema import schema
from app.supplier_quotes.schemas import (
    CanonicalQuoteRow,
    SupplierInput,
)
from app.supplier_quotes.service import SupplierQuoteService

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Supplier Quote Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(organization_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=organization_id,
        user_id=None,
        role=MembershipRole.OWNER,
        request_id="supplier-quote-test",
    )


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    response = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["organization_id"])


async def test_supplier_graphql_schema_has_safe_quote_workflows() -> None:
    sdl = schema.as_str()

    for field in (
        "gmailConnection",
        "suppliers",
        "pendingSupplierMessages",
        "pendingSupplierMappings",
        "supplierReports",
        "supplierReport",
        "createSupplier",
        "updateSupplier",
        "confirmSupplierQuoteMatch",
    ):
        assert field in sdl
    # Mapping confirmation is REST (commit, then queue); it must not also exist here.
    for forbidden in (
        "refreshToken",
        "refreshTokenCiphertext",
        "rawEmailBody",
        "attachmentBytes",
        "confirmSupplierMapping",
    ):
        assert forbidden not in sdl


async def test_supplier_quote_queries_require_authentication(api_client: AsyncClient) -> None:
    response = await graphql(
        api_client,
        """
        query {
          gmailConnection { connected }
          suppliers { id name }
          pendingSupplierMessages { id status }
          pendingSupplierMappings { id version }
          supplierReports { id profitStatus }
        }
        """,
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_supplier_create_update_and_list_are_tenant_scoped(
    api_client: AsyncClient, clean_users
) -> None:
    token_a = await _register(api_client, "supplier-a@example.com", "Supplier Org A")
    org_a = await _organization_id(api_client, token_a)
    token_b = await _register(api_client, "supplier-b@example.com", "Supplier Org B")
    org_b = await _organization_id(api_client, token_b)

    create = await graphql(
        api_client,
        """
        mutation CreateSupplier($input: SupplierInput!) {
          createSupplier(input: $input) {
            id name senderEmails senderDomains isActive
          }
        }
        """,
        {
            "input": {
                "name": "Acme Industrial",
                "senderEmails": ["Quotes@Acme.example"],
                "senderDomains": ["acme.example"],
            }
        },
        _auth(token_a),
    )
    assert "errors" not in create.json(), create.json()
    supplier = create.json()["data"]["createSupplier"]
    supplier_id = uuid.UUID(supplier["id"])
    assert supplier["senderEmails"] == ["quotes@acme.example"]

    update = await graphql(
        api_client,
        """
        mutation UpdateSupplier($id: UUID!, $input: SupplierInput!) {
          updateSupplier(id: $id, input: $input) { id name senderDomains isActive }
        }
        """,
        {
            "id": str(supplier_id),
            "input": {
                "name": "Acme Components",
                "senderEmails": ["offers@acme.example"],
                "senderDomains": ["quotes.acme.example"],
                "isActive": True,
            },
        },
        _auth(token_a),
    )
    assert update.json()["data"]["updateSupplier"]["name"] == "Acme Components"

    scoped = await graphql(api_client, "query { suppliers { id name } }", headers=_auth(token_b))
    assert scoped.json()["data"]["suppliers"] == []
    assert org_a != org_b


async def test_pending_mapping_lists_every_sheet_it_can_move_to(
    api_client: AsyncClient, clean_users
) -> None:
    token = await _register(api_client, "mapping-review@example.com", "Mapping Review Org")
    org_id = await _organization_id(api_client, token)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        supplier = await SupplierQuoteService(session, tenant).create_supplier(
            SupplierInput(name="Acme")
        )
        connection = GmailConnection(
            organization_id=org_id,
            email="quotes@example.com",
            refresh_token_ciphertext="test-only-ciphertext",
            history_id="50",
        )
        session.add(connection)
        await session.flush()
        service = SupplierQuoteService(session, tenant)
        message, _ = await service.get_or_create_message(
            connection_id=connection.id,
            gmail_message_id="gmail-message-internal",
            history_id="50",
            sender_email="quotes@example.com",
            received_at=datetime.now(UTC),
            attachment_ids=[{"attachment_id": "gmail-attachment-internal"}],
        )
        proposal = await service.save_mapping_proposal(
            supplier.id,
            worksheet_name="Quote",
            header_row=1,
            column_mapping={"product_name": "Item", "unit_price": "Price"},
            column_labels=["Item", "Price", "Currency", "Unit"],
            source_message_id=message.id,
            source_attachment_id="gmail-attachment-internal",
            sheet_options=[
                {"worksheet_name": "Quote", "header_row": 1, "labels": ["Item", "Price"]},
                {"worksheet_name": "Totals", "header_row": 4, "labels": ["Sum"]},
            ],
        )
        assert proposal.id is not None

    pending = await graphql(
        api_client,
        "query { pendingSupplierMappings { id sheetOptions { worksheetName headerRow labels } } }",
        headers=_auth(token),
    )
    [listed] = pending.json()["data"]["pendingSupplierMappings"]
    assert listed["sheetOptions"] == [
        {"worksheetName": "Quote", "headerRow": 1, "labels": ["Item", "Price"]},
        {"worksheetName": "Totals", "headerRow": 4, "labels": ["Sum"]},
    ]


async def test_supplier_report_detail_contains_ranked_quote_rows(
    api_client: AsyncClient, clean_users
) -> None:
    token = await _register(api_client, "supplier-report@example.com", "Report Org")
    org_id = await _organization_id(api_client, token)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = SupplierQuoteService(session, tenant)
        supplier = await service.create_supplier(SupplierInput(name="Lowest Cost"))
        connection = GmailConnection(
            organization_id=org_id,
            email="quotes@example.com",
            refresh_token_ciphertext="test-only-ciphertext",
            history_id="50",
        )
        session.add(connection)
        await session.flush()
        message, _ = await service.get_or_create_message(
            connection_id=connection.id,
            gmail_message_id="report-message-internal",
            history_id="50",
            sender_email="quotes@example.com",
            received_at=datetime.now(UTC),
            attachment_ids=[{"attachment_id": "report-attachment", "filename": "quote.xlsx"}],
        )
        message.supplier_id = supplier.id
        upsert = await service.upsert_quote_rows(
            supplier.id,
            message.id,
            "report-attachment",
            [
                CanonicalQuoteRow(
                    worksheet_name="Quote",
                    source_row_number=2,
                    supplier_sku="SKU-1",
                    product_name="Widget",
                    unit_price=Decimal("10.00"),
                    currency="USD",
                    purchase_unit="each",
                )
            ],
        )
        report_id = upsert.report.id

    response = await graphql(
        api_client,
        """
        query SupplierReport($id: UUID!) {
          supplierReport(id: $id) {
            report {
              id profitStatus summaryJson senderEmail receivedAt supplierId bestProfitCurrency
            }
            attachments { id filename }
            quotes {
              id supplierId productName unitPrice currency purchaseUnit rankingBasis
              attachmentId worksheetName sourceRowNumber matchStatus
              catalogSalePrice catalogSaleCurrency catalogSaleUnit catalogSalePriceUpdatedAt
            }
          }
        }
        """,
        {"id": str(report_id)},
        _auth(token),
    )
    body = response.json()
    assert "errors" not in body, body
    detail = body["data"]["supplierReport"]
    [quote] = detail["quotes"]
    assert quote["supplierId"] == str(supplier.id)
    assert quote["productName"] == "Widget"
    assert quote["unitPrice"] == "10.0000"
    # Audit trail back to the source cell (spec §8).
    assert (quote["attachmentId"], quote["worksheetName"], quote["sourceRowNumber"]) == (
        "report-attachment",
        "Quote",
        2,
    )
    assert quote["matchStatus"] == "unmatched"
    assert detail["attachments"] == [{"id": "report-attachment", "filename": "quote.xlsx"}]
    assert detail["report"]["senderEmail"] == "quotes@example.com"
    assert detail["report"]["supplierId"] == str(supplier.id)
    assert detail["report"]["receivedAt"] is not None


async def test_an_owner_accepts_a_suggested_product_match(
    api_client: AsyncClient, clean_users
) -> None:
    from decimal import Decimal as D

    from app.db.models import Product

    token = await _register(api_client, "match-review@example.com", "Match Review Org")
    org_id = await _organization_id(api_client, token)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = SupplierQuoteService(session, tenant)
        supplier = await service.create_supplier(SupplierInput(name="Acme"))
        product = Product(
            organization_id=org_id,
            external_id="CAT-1",
            name="Catalogue Widget",
            slug="catalogue-widget",
            price=D("20.00"),
            currency="USD",
            price_updated_at=datetime.now(UTC),
            attributes={"sale_unit": "each"},
        )
        session.add(product)
        connection = GmailConnection(
            organization_id=org_id,
            email="quotes@example.com",
            refresh_token_ciphertext="test-only-ciphertext",
            history_id="50",
        )
        session.add(connection)
        await session.flush()
        message, _ = await service.get_or_create_message(
            connection_id=connection.id,
            gmail_message_id="match-message",
            history_id="50",
            sender_email="quotes@example.com",
            received_at=datetime.now(UTC),
            attachment_ids=[{"attachment_id": "part:1"}],
        )
        upsert = await service.upsert_quote_rows(
            supplier.id,
            message.id,
            "part:1",
            [
                CanonicalQuoteRow(
                    worksheet_name="Quote",
                    source_row_number=2,
                    supplier_sku="ACME-1",
                    product_name="Widget",
                    unit_price=Decimal("10.00"),
                    currency="USD",
                    purchase_unit="each",
                )
            ],
        )
        [quote] = upsert.quotes
        quote.product_id = product.id
        quote.match_status = "suggested"
        report_id, quote_id, product_id = upsert.report.id, quote.id, product.id

    detail = await graphql(
        api_client,
        """
        query SupplierReport($id: UUID!) {
          supplierReport(id: $id) { quotes { id catalogProductName catalogProductSku } }
        }
        """,
        {"id": str(report_id)},
        _auth(token),
    )
    [listed] = detail.json()["data"]["supplierReport"]["quotes"]
    assert (listed["catalogProductName"], listed["catalogProductSku"]) == (
        "Catalogue Widget",
        "CAT-1",
    )

    confirmed = await graphql(
        api_client,
        """
        mutation ConfirmSupplierQuoteMatch($quoteId: UUID!, $productId: UUID!) {
          confirmSupplierQuoteMatch(quoteId: $quoteId, productId: $productId) {
            id matchStatus unitGrossProfit purchasePriceRank
          }
        }
        """,
        {"quoteId": str(quote_id), "productId": str(product_id)},
        _auth(token),
    )
    body = confirmed.json()
    assert "errors" not in body, body
    assert body["data"]["confirmSupplierQuoteMatch"]["matchStatus"] == "confirmed"
    assert body["data"]["confirmSupplierQuoteMatch"]["purchasePriceRank"] == 1
