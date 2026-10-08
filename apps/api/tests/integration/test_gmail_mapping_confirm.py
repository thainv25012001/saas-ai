"""Confirming a mapping proposal over REST: commit first, then queue the re-fetch."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.auth.dependencies import get_current_tenant
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import GmailConnection, MembershipRole
from app.db.models.supplier import SupplierMapping
from app.supplier_quotes.schemas import SupplierInput
from app.supplier_quotes.service import SupplierQuoteService

pytestmark = pytest.mark.anyio

_BODY = {
    "worksheet_name": "Quote",
    "header_row": 1,
    "columns": {"product_name": "Item", "unit_price": "Price", "currency": "Currency"},
    "default_purchase_unit": "each",
}


def _app(tenant: TenantContext) -> FastAPI:
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    return app


async def _proposal(tenant: TenantContext) -> tuple[uuid.UUID, uuid.UUID]:
    async with tenant_session(tenant) as session:
        service = SupplierQuoteService(session, tenant)
        supplier = await service.create_supplier(SupplierInput(name="Acme"))
        connection = GmailConnection(
            organization_id=tenant.organization_id,
            email="buyer@example.com",
            refresh_token_ciphertext="test-only-ciphertext",
            history_id="50",
        )
        session.add(connection)
        await session.flush()
        message, _ = await service.get_or_create_message(
            connection_id=connection.id,
            gmail_message_id="gmail-message",
            history_id="50",
            sender_email="quotes@acme.example",
            received_at=datetime.now(UTC),
            attachment_ids=[{"attachment_id": "part:1"}],
        )
        proposal = await service.save_mapping_proposal(
            supplier.id,
            worksheet_name="Quote",
            header_row=1,
            column_mapping={"product_name": "Item"},
            column_labels=["Item", "Price", "Currency"],
            source_message_id=message.id,
            source_attachment_id="part:1",
        )
        return proposal.id, message.id


async def test_confirming_commits_the_mapping_before_queueing_the_refetch(
    tenant_a: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import gmail as gmail_api

    proposal_id, message_id = await _proposal(tenant_a)
    seen_at_enqueue: list[tuple[str, bool]] = []

    async def enqueue(organization_id: str, supplier_message_id: str, mapping_id: str) -> None:
        # A separate session, as the worker would have: the confirmation must
        # already be committed when the job is handed over.
        async with tenant_session(tenant_a) as session:
            mapping = await session.scalar(
                select(SupplierMapping).where(SupplierMapping.id == uuid.UUID(mapping_id))
            )
            seen_at_enqueue.append((supplier_message_id, bool(mapping and mapping.is_confirmed)))

    monkeypatch.setattr(gmail_api, "enqueue_supplier_message_reprocess", enqueue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tenant_a)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/gmail/supplier-mappings/{proposal_id}/confirm", json=_BODY
        )
        again = await client.post(
            f"/api/v1/gmail/supplier-mappings/{proposal_id}/confirm", json=_BODY
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"mapping_id": str(proposal_id), "queued": True}
    assert seen_at_enqueue == [(str(message_id), True)]
    assert again.status_code == 409


async def test_only_the_owner_can_confirm_a_mapping(
    tenant_a: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import gmail as gmail_api

    proposal_id, _ = await _proposal(tenant_a)

    async def enqueue(*_args: Any) -> None:
        raise AssertionError("a refused confirmation must not queue anything")

    monkeypatch.setattr(gmail_api, "enqueue_supplier_message_reprocess", enqueue)
    member = dataclasses.replace(tenant_a, role=MembershipRole.MEMBER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(member)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/gmail/supplier-mappings/{proposal_id}/confirm", json=_BODY
        )

    assert response.status_code == 403


async def test_queue_it_again_is_never_swallowed_by_an_earlier_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import gmail as gmail_api

    calls: list[dict[str, Any]] = []

    class _Pool:
        async def enqueue_job(self, _name: str, **kwargs: Any) -> object:
            calls.append(kwargs)
            return object()

        async def aclose(self) -> None:
            return None

    async def create_pool(*_args: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr(gmail_api, "create_pool", create_pool)

    await gmail_api.enqueue_supplier_message_reprocess("org", "message", "mapping")
    await gmail_api.enqueue_supplier_message_reprocess("org", "message", "mapping")

    # arq keeps a finished job's result for an hour, and a fixed `_job_id`
    # makes every retry inside that hour a silent no-op.
    assert len(calls) == 2
    assert all("_job_id" not in call for call in calls)
