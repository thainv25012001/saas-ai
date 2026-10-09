"""Migration 0022 moves agents off the removed `fake` provider.

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
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0022_agents_off_fake.py"
)


def _migration_statement() -> str:
    spec = importlib.util.spec_from_file_location("migration_0022", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.MOVE_FAKE_AGENTS)


async def _agent(
    owner_connection: Any, tenant: TenantContext, slug: str, provider: str, model: str
) -> uuid.UUID:
    agent_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO agents "
            "(id, organization_id, name, slug, status, provider, model, "
            " temperature, max_tokens, public_key) "
            "VALUES (:id, :org, :name, :slug, 'draft', :provider, :model, 0.3, 1024, :key)"
        ),
        {
            "id": agent_id,
            "org": tenant.organization_id,
            "name": slug,
            "slug": slug,
            "provider": provider,
            "model": model,
            "key": f"pk_test_{agent_id.hex[:24]}",
        },
    )
    return agent_id


async def test_fake_agents_move_to_the_default_openrouter_model(
    owner_connection: Any, tenant_a: TenantContext
) -> None:
    offline = await _agent(owner_connection, tenant_a, "offline-bot", "fake", "fake-1")
    openai = await _agent(owner_connection, tenant_a, "openai-bot", "openai", "gpt-4o-mini")
    await owner_connection.commit()
    try:
        await owner_connection.execute(text(_migration_statement()))
        result = await owner_connection.execute(
            text("SELECT id, provider, model FROM agents WHERE id IN (:a, :b)"),
            {"a": offline, "b": openai},
        )
        after = {row[0]: (row[1], row[2]) for row in result.all()}
    finally:
        await owner_connection.rollback()

    assert after[offline] == ("openrouter", "google/gemma-4-31b-it:free")
    assert after[openai] == ("openai", "gpt-4o-mini")
