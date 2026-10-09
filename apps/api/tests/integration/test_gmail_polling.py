"""Polling's fan-out against the real database and Redis.

Rows go into the shared dev database; `tenant_a`/`tenant_b` teardown deletes
their organizations, which cascades to these `gmail_connections` rows.
Redis jobs go to a queue no real worker reads, so a running dev worker
cannot consume one between the two enqueues a test compares.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import job_key_prefix
from sqlalchemy import text

from app.core.config import get_settings
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.gmail.polling import (
    PollFanOut,
    enqueue_mailbox_polls,
    gmail_poll_job_id,
    list_polled_mailboxes,
)

pytestmark = pytest.mark.anyio

_JOB = "poll_gmail_mailbox_task"


def _address() -> str:
    return f"poll-{uuid.uuid4().hex[:12]}@example.com"


async def _connect(owner_connection: Any, tenant: TenantContext, email: str, status: str) -> None:
    await owner_connection.execute(
        text(
            "INSERT INTO gmail_connections "
            "(id, organization_id, email, refresh_token_ciphertext, history_id, status) "
            "VALUES (:id, :org, :email, :token, '100', :status)"
        ),
        {
            "id": uuid7(),
            "org": tenant.organization_id,
            "email": email,
            # ck_gmail_connections_connected_has_credential: only a
            # disconnected row may lack a credential.
            "token": None if status == "disconnected" else "ciphertext",
            "status": status,
        },
    )
    await owner_connection.commit()


@pytest.fixture
async def arq_pool() -> AsyncIterator[ArqRedis]:
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        yield pool
    finally:
        await pool.aclose()


@pytest.fixture
def queue() -> str:
    return f"test-gmail-poll-{uuid.uuid4().hex}"


async def _forget(pool: ArqRedis, email: str, queue: str) -> None:
    job_id = gmail_poll_job_id(email)
    await pool.zrem(queue, job_id)
    await pool.delete(job_key_prefix + job_id)


async def test_a_mailbox_connected_by_two_organizations_is_polled_once(
    owner_connection: Any, tenant_a: TenantContext, tenant_b: TenantContext
) -> None:
    email = _address()
    await _connect(owner_connection, tenant_a, email, "connected")
    await _connect(owner_connection, tenant_b, email, "connected")

    polled = await list_polled_mailboxes()

    assert polled.count(email) == 1


@pytest.mark.parametrize("status", ["needs_reauth", "disconnected"])
async def test_a_mailbox_that_cannot_sync_is_not_polled(
    owner_connection: Any, tenant_a: TenantContext, status: str
) -> None:
    email = _address()
    await _connect(owner_connection, tenant_a, email, status)

    assert email not in await list_polled_mailboxes()


async def test_a_mailbox_with_a_poll_pending_is_not_queued_again(
    arq_pool: ArqRedis, queue: str
) -> None:
    email = _address()
    try:
        first = await enqueue_mailbox_polls(arq_pool, [email], job_name=_JOB, queue_name=queue)
        second = await enqueue_mailbox_polls(arq_pool, [email], job_name=_JOB, queue_name=queue)
    finally:
        await _forget(arq_pool, email, queue)

    assert first == PollFanOut(enqueued=1, already_pending=0)
    assert second == PollFanOut(enqueued=0, already_pending=1)
