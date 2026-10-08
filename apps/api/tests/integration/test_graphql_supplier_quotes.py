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
        "confirmSupplierMapping",
    ):
        assert field in sdl
    for forbidden in ("refreshToken", "refreshTokenCiphertext", "rawEmailBody", "attachmentBytes"):
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


async def test_pending_mapping_can_be_confirmed_for_its_source_message(
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
        )
        proposal_id = proposal.id
        source_message_id = message.id

    response = await graphql(
        api_client,
        """
        mutation ConfirmSupplierMapping($id: UUID!, $input: ConfirmSupplierMappingInput!) {
          confirmSupplierMapping(id: $id, input: $input) {
            id version isConfirmed sourceMessageId worksheetName headerRow
            columns { field label }
          }
        }
        """,
        {
            "id": str(proposal_id),
            "input": {
                "worksheetName": "Quote",
                "headerRow": 1,
                "columns": [
                    {"field": "PRODUCT_NAME", "label": "Item"},
                    {"field": "UNIT_PRICE", "label": "Price"},
                    {"field": "CURRENCY", "label": "Currency"},
                    {"field": "UNIT", "label": "Unit"},
                ],
            },
        },
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    mapping = body["data"]["confirmSupplierMapping"]
    assert mapping["id"] == str(proposal_id)
    assert mapping["isConfirmed"] is True
    assert mapping["sourceMessageId"] == str(source_message_id)


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
            attachment_ids=[{"attachment_id": "report-attachment"}],
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
            report { id profitStatus summaryJson }
            quotes { id supplierId productName unitPrice currency purchaseUnit rankingBasis }
          }
        }
        """,
        {"id": str(report_id)},
        _auth(token),
    )
    body = response.json()
    assert "errors" not in body, body
    [quote] = body["data"]["supplierReport"]["quotes"]
    assert quote["supplierId"] == str(supplier.id)
    assert quote["productName"] == "Widget"
    assert quote["unitPrice"] == "10.0000"
