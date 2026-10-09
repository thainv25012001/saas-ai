"""Polling mode's trigger: find the connected mailboxes, queue one sync each.

The sync itself is `run_gmail_sync`, unchanged. Polling replaces only what
starts it (docs/PHASE-9.md §2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from arq.connections import ArqRedis
from sqlalchemy import text

from app.core.tenancy import untenanted_session


@dataclass(frozen=True, slots=True)
class PollFanOut:
    enqueued: int
    already_pending: int


def gmail_poll_job_id(mailbox_email: str) -> str:
    """One fixed job ID per mailbox.

    With the poll task registered `keep_result=0`, arq refuses a second
    enqueue while this mailbox's poll is queued, running or waiting to retry,
    and accepts the next one once it ends (arq 0.28 `Worker.finish_job`
    deletes the job key when no result is kept). Hashed like
    `gmail_sync_job_id`, so the address is not a Redis key in clear.
    """
    normalized = mailbox_email.strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]
    return f"gmail-poll:{digest}"


async def list_polled_mailboxes() -> list[str]:
    """Every connected mailbox address, once.

    Read through the SECURITY DEFINER resolver the maintenance job uses: the
    worker has no tenant yet, and RLS shows an untenanted session no rows.
    It returns `connected` rows only, so a `needs_reauth` mailbox is not
    sent to Google with a dead refresh token every interval. One address
    connected by several organizations is polled once; `run_gmail_sync`
    already syncs every organization connected to it.
    """
    async with untenanted_session() as session:
        result = await session.execute(
            text("SELECT email FROM list_gmail_connections_for_maintenance()")
        )
        emails = [str(row[0]).strip().casefold() for row in result.all()]
    return list(dict.fromkeys(emails))


async def enqueue_mailbox_polls(
    redis: ArqRedis,
    mailbox_emails: list[str],
    *,
    job_name: str,
    queue_name: str | None = None,
) -> PollFanOut:
    """Queue `job_name` once per mailbox, skipping mailboxes still pending.

    `job_name` is passed in, not imported: the task lives in
    `app.workers.tasks`, which imports this module.
    """
    enqueued = 0
    for email in mailbox_emails:
        job = await redis.enqueue_job(
            job_name,
            mailbox_email=email,
            _job_id=gmail_poll_job_id(email),
            _queue_name=queue_name,
        )
        if job is not None:
            enqueued += 1
    return PollFanOut(enqueued=enqueued, already_pending=len(mailbox_emails) - enqueued)
