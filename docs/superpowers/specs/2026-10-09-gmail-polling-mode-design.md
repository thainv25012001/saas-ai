# Gmail Polling Mode

**Status:** Draft for user review  
**Date:** 2026-10-09  
**Builds on:** `2026-10-08-gmail-supplier-quote-analysis-design.md`, `docs/PHASE-9.md`

## 1. Goal

Let the Gmail supplier quote pipeline pick up new mail by polling Gmail on a
schedule, as an alternative to the Pub/Sub push it uses today. Polling needs no
Google Cloud topic, no push subscription and no public HTTPS endpoint, so it
works in local development and in deployments that have not set up Pub/Sub.

Polling becomes the default mode. Pub/Sub stays available behind a setting.

**Success criteria**

- With no `GMAIL_PUBSUB_*` setting at all, an owner can connect Gmail, and a
  supplier email with an `.xlsx` attachment reaches the dashboard within one
  poll interval (5 minutes by default) plus processing time.
- With `GMAIL_SYNC_MODE=pubsub`, behaviour is exactly what it is today.
- Each mailbox is polled by at most one job at a time, however many workers run.

## 2. Existing context

- `POST /api/v1/gmail/pubsub` verifies a push and enqueues
  `sync_gmail_mailbox_task`, which calls `run_gmail_sync`. That resolves every
  organization connected to the mailbox, takes a per-connection Redis lock and
  reads `history.list` from the stored `history_id` cursor (`_sync_connection`).
  Message processing is idempotent per message and attachment.
- A daily 03:00 UTC cron (`renew_gmail_watches_task` → `run_gmail_maintenance`)
  renews watches that expire within a day and forces one history sync per
  mailbox.
- The OAuth callback refuses to run without `GMAIL_PUBSUB_TOPIC` and takes the
  starting cursor and `watch_expires_at` from `users.watch`.
- The dashboard card already shows "Last checked" from `last_history_sync_at`.

Polling reuses `_sync_connection` unchanged. Only the trigger is new.

## 3. Decisions

| Decision | Choice | Reason |
| --- | --- | --- |
| Relationship to Pub/Sub | Alternative mode, selected by config | Pub/Sub stays usable for low-latency production setups. |
| Scope of the switch | Whole deployment (env var) | No per-organization need; avoids a schema and UI change. |
| Default mode | `polling` | Works with no Google Cloud setup. |
| Default interval | 5 minutes | Supplier price lists do not need sub-minute latency. |
| Trigger mechanism | arq cron fan-out, one job per mailbox | One slow mailbox does not delay the others; arq cron already runs once across workers. |

Rejected alternatives: a single cron job that syncs every mailbox in sequence
(one large workbook with LLM matching delays every tenant, and the sweep must fit
inside the interval); a long-running poll loop started with the worker (runs once
per worker replica and dies silently with the process).

## 4. Configuration

New settings in `app/core/config.py`:

- `gmail_sync_mode: Literal["pubsub", "polling"] = "polling"`
- `gmail_poll_interval_minutes: int = 5`, between 1 and 60 and a divisor of 60.
  arq cron takes a set of minutes (`{0, 5, 10, …}`), so a value like 7 would
  leave an uneven gap at each hour. Settings validation rejects it at startup.

Both are passed to the `api` and `worker` services in `docker-compose.yml` and
documented in `.env.example`.

In polling mode the three `GMAIL_PUBSUB_*` settings are not required. The
webhook route stays mounted and keeps verifying every request. A push that still
arrives (for example from a watch created before the switch) enqueues the same
idempotent sync.

**Upgrade note:** because the default changes, a deployment that runs Pub/Sub
today must set `GMAIL_SYNC_MODE=pubsub` explicitly to keep its behaviour.
`render.yaml` sets no Gmail values, so nothing in the repository depends on the
old default.

## 5. Connect, disconnect and daily maintenance

**`_require_oauth_settings`** requires `GMAIL_PUBSUB_TOPIC` only in `pubsub`
mode. The callback's separate topic check follows the same rule.

**OAuth callback.** A helper returns the starting `(history_id,
watch_expires_at)` for the mode:

- `pubsub`: call `users.watch`, as today.
- `polling`: use `profile.history_id` from the `get_profile` call the callback
  already makes, with `watch_expires_at = None`. No watch is created.

The reconnect rules are unchanged: the cursor resets when the mailbox address
changes, when the connection was disconnected, or when it has no cursor. The
cleanup `stop_watch` after a failed save runs only if this callback started a
watch. The `stop_watch` on the previous mailbox, when the address changes, runs
in both modes, because that mailbox may still hold a watch from Pub/Sub mode.

**Disconnect** is unchanged. It already calls `stop_watch` best-effort and
ignores the failure.

**Cron jobs are chosen by mode.** `pubsub` registers the 03:00 renewal cron, as
today. `polling` registers the poll cron instead and not the 03:00 cron. The
03:00 job's sync is redundant there, and its renewal step would treat
`watch_expires_at = None` as "renew now" and mark every connection
`gmail_topic_not_configured`.

**Switching modes**

- Pub/Sub → polling: existing watches expire on their own within 7 days. Pushes
  that arrive until then are harmless.
- Polling → Pub/Sub: connections have no watch until the next 03:00 renewal, so
  no pushes arrive for up to a day. The docs tell the operator to reconnect Gmail
  or wait.

## 6. Worker

**`poll_gmail_mailboxes_task(ctx)`** is the cron job. It runs at minutes
`set(range(0, 60, interval))`, with `max_tries=1`, a 60-second timeout and
`run_at_startup=True`, so a freshly started worker syncs without waiting for
the next slot.

1. List mailboxes through the existing SECURITY DEFINER function
   `list_gmail_connections_for_maintenance()`. It returns only
   `status = 'connected'`, so `needs_reauth` and `disconnected` connections are
   not polled, and a dead refresh token is not sent to Google every interval.
2. Deduplicate by address. `run_gmail_sync` already handles every organization
   connected to the same mailbox.
3. Enqueue `poll_gmail_mailbox_task(mailbox_email=…)` per address through
   `ctx["redis"]` with `_job_id=gmail_poll_job_id(email)`. The ID is
   `gmail-poll:` plus a truncated sha256 of the case-folded address, like
   `gmail_sync_job_id`, so the address is not a Redis key in clear.
4. Log `gmail_poll_started` with `mailboxes`, `enqueued` and `already_pending`.

**`poll_gmail_mailbox_task(ctx, *, mailbox_email)`** is registered with
`func(..., keep_result=0, max_tries=3)` and calls
`run_gmail_sync(mailbox_email=…, notification_history_id=None, force_sync=True,
skip_if_busy=True)`.

The fixed job ID with `keep_result=0` gives at most one poll per mailbox queued
or running. arq refuses an enqueue while the job key or result key exists. With
no result kept, arq 0.28 `Worker.finish_job` deletes the job key when the job
ends, so the next interval can enqueue again. A poll that exhausts its retries
goes through `finish_failed_job`, which stores a result for the worker-wide
`keep_result_s` (an hour) regardless of the per-function setting, so the fan-out
deletes `arq:result:<job_id>` before each enqueue. A retry (`Retry`) keeps the job
key, so polls do not pile up behind a struggling mailbox.

**`run_gmail_sync(..., skip_if_busy: bool = False)`.** When the mailbox lock is
held and `skip_if_busy` is true, it logs `gmail_sync_busy_skipped` and moves on
instead of raising `Retry`. The next poll covers the mailbox. The Pub/Sub path
keeps `skip_if_busy=False`, because a notification can name a change that the
running job listed too early to see.

## 7. Error handling

The existing per-connection behaviour applies unchanged:

| Failure | Result |
| --- | --- |
| Refresh token rejected, or Gmail 401 | Connection becomes `needs_reauth`, drops out of polling until the owner reconnects. |
| Gmail 429 or 5xx | `Retry(60s)`, at most 3 tries; the cursor stays put and the next poll resumes from it. |
| History cursor expired (404) | Existing reconciliation through `messages.list` `after:last_sync`. |
| One message fails to process | Recorded on that message; the mailbox keeps moving. |
| Mailbox busy | Skipped this interval (`gmail_sync_busy_skipped`). |

No new error codes or schema changes.

## 8. Testing

Tests are written first. Integration tests use the host-run overrides: localhost
Postgres and Redis, and the non-superuser runtime role so RLS applies.

Unit:

- Settings accept `polling` and `pubsub`, default to `polling`, default the
  interval to 5, and reject intervals that do not divide 60 or fall outside 1–60.
- Cron jobs per mode: `polling` has the poll cron at the expected minutes and no
  03:00 cron; `pubsub` has the 03:00 cron and no poll cron.
- `poll_gmail_mailbox_task` is registered with `keep_result=0`.
- `gmail_poll_job_id` is stable and ignores address case and surrounding spaces.
- OAuth callback in polling mode with no topic configured: never calls `watch`,
  stores the profile `historyId`, leaves `watch_expires_at` empty.
- `_require_oauth_settings` does not require a topic in polling mode and still
  does in `pubsub` mode.

Integration:

- The fan-out enqueues one job per connected mailbox address and skips
  `needs_reauth` and `disconnected` connections.
- A second fan-out while a mailbox's poll job is still queued enqueues nothing
  for that mailbox.
- `run_gmail_sync(skip_if_busy=True)` on a locked mailbox returns without
  raising `Retry`; without the flag it still raises `Retry`.
- An end-to-end poll processes a new supplier message through the stubbed Gmail
  fixtures in `tests/integration/test_gmail_pipeline.py`.

## 9. Documentation

- `docs/PHASE-9.md`: a "Polling mode" section covering when to use each mode,
  the two settings, the default change, and the mode-switch caveats from §5.
  The Google Cloud setup steps are marked as needed only for `pubsub` mode.
- `.env.example` and `docker-compose.yml`: the two new settings.
- `README.md`: the Phase 9 row mentions polling as the default trigger.
- No UI change, so `docs/DESIGN.md` is not touched.

## 10. Out of scope

- Per-organization choice of mode.
- Caching access tokens between polls. One token refresh per mailbox per
  interval is well within Google's limits.
- Backfilling mail that arrived while a mailbox was disconnected (unchanged).
