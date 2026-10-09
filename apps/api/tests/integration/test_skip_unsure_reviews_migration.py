"""Migration 0021 retires the unsure-classification reviews already queued.

Its UPDATE runs here against the real table inside a transaction that is rolled
back, so other rows in the shared dev database are left as they were.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import TenantContext

pytestmark = pytest.mark.anyio

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0021_skip_unsure_reviews.py"
)


def _migration_statement() -> str:
    spec = importlib.util.spec_from_file_location("migration_0021", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.SKIP_UNSURE_REVIEWS)


async def _seed(owner_connection: Any, tenant: TenantContext) -> uuid.UUID:
    connection_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO gmail_connections "
            "(id, organization_id, email, refresh_token_ciphertext, history_id, status) "
            "VALUES (:id, :org, :email, 'ciphertext', '100', 'connected')"
        ),
        {
            "id": connection_id,
            "org": tenant.organization_id,
            "email": f"inbox-{uuid.uuid4().hex[:8]}@example.com",
        },
    )
    rows = {
        "uncertain": ("review_required", "supplier_classification_review", "uncertain", 0.0),
        "low-confidence": ("review_required", "supplier_classification_review", "supplier", 0.6),
        "confident": ("review_required", "supplier_classification_review", "supplier", 0.95),
        "rows-to-check": ("review_required", "items_need_review", "supplier", 1.0),
        "failed": ("failed", "processing_failed", "uncertain", 0.0),
    }
    for gmail_id, (status, error_code, classification, confidence) in rows.items():
        await owner_connection.execute(
            text(
                "INSERT INTO supplier_messages "
                "(id, organization_id, gmail_connection_id, gmail_message_id, sender_email, "
                "attachment_ids, status, classification, classification_confidence, error_code) "
                "VALUES (:id, :org, :conn, :gmail_id, 'someone@example.com', '[]'::jsonb, "
                ":status, :classification, :confidence, :error_code)"
            ),
            {
                "id": uuid7(),
                "org": tenant.organization_id,
                "conn": connection_id,
                "gmail_id": gmail_id,
                "status": status,
                "classification": classification,
                "confidence": confidence,
                "error_code": error_code,
            },
        )
    await owner_connection.commit()
    return connection_id


async def test_unsure_classification_reviews_become_ignored(
    owner_connection: Any, tenant_a: TenantContext
) -> None:
    connection_id = await _seed(owner_connection, tenant_a)
    try:
        await owner_connection.execute(text(_migration_statement()))
        result = await owner_connection.execute(
            text(
                "SELECT gmail_message_id, status, error_code, processed_at IS NOT NULL "
                "FROM supplier_messages WHERE gmail_connection_id = :conn"
            ),
            {"conn": connection_id},
        )
        after = {row[0]: (row[1], row[2], row[3]) for row in result.all()}
    finally:
        await owner_connection.rollback()

    assert after["uncertain"] == ("ignored", None, True)
    assert after["low-confidence"] == ("ignored", None, True)
    # A confident classification, rows to check and failures stay for the owner.
    assert after["confident"][:2] == ("review_required", "supplier_classification_review")
    assert after["rows-to-check"][:2] == ("review_required", "items_need_review")
    assert after["failed"][:2] == ("failed", "processing_failed")
