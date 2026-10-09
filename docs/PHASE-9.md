# Phase 9 — Gmail supplier quote analysis

> Built from [the design](superpowers/specs/2026-10-08-gmail-supplier-quote-analysis-design.md)
> and [the plan](superpowers/plans/2026-10-08-gmail-supplier-quotes.md). This document
> records what was delivered, how to configure it, and where it departs from the design.

**Goal:** when a supplier emails an `.xlsx` quote to the organization's Gmail, the SaaS
reads it, normalizes whatever layout the supplier uses, compares each offer against the
catalogue, and shows the result in the dashboard. No one forwards or re-types anything.

```text
polling (default): arq cron every GMAIL_POLL_INTERVAL_MINUTES ─┐  one job per connected mailbox
pubsub:  Gmail (watch) ──▶ Pub/Sub ──authenticated push──▶ POST /api/v1/gmail/pubsub
                                                           │ verify OIDC token,
                                                           │ resolve mailbox → tenant
                                                           ▼
                                         arq: poll_gmail_mailbox_task / sync_gmail_mailbox_task
                                           history pages since the saved cursor
                                           → new inbox messages from known suppliers
                                           → fetch .xlsx attachments (in memory)
                                           → mapping: confirmed? parse : propose + review
                                           → match catalogue, rank, persist report
                                                           │
                                                           ▼
                                 /dashboard/supplier-quotes (GraphQL)
```

## 1. Scope

Delivered:

- Gmail OAuth (read-only scope), with the refresh token encrypted at rest (AES-256-GCM).
  Owners connect and disconnect. Disconnecting stops the watch and deletes the stored
  credential, but keeps the mailbox row, so supplier messages, quotes and reports remain.
  Reconnecting starts from the new watch: mail received while disconnected is not
  backfilled.
- **Polling (default).** Every `GMAIL_POLL_INTERVAL_MINUTES` (5 by default) an arq cron
  queues one sync per connected mailbox, with at most one in flight per mailbox. It needs
  no Google Cloud Pub/Sub and no public URL.
- **Pub/Sub push (`GMAIL_SYNC_MODE=pubsub`).** Every push is verified (signature, issuer,
  audience, service account and verified email) before any database lookup happens.
- Mailbox sync from the watch cursor onward. Pub/Sub redelivery and overlapping history
  pages never duplicate a message or a quote row.
- **Failure handling:**
  - A message that fails for a reason a retry would not change is recorded as `failed`, and
    the cursor still advances. One bad email never stops a mailbox.
  - Transient Gmail errors (429/5xx) and a mailbox busy with another job retry the job
    (`arq.Retry`, up to 20 tries for Gmail jobs).
  - The daily maintenance job isolates each mailbox, so one tenant's failure does not stop
    renewal for the rest.
- Workbook inspection and parsing (`openpyxl`, read-only and data-only, so formulas are
  never evaluated), with row-level issues that leave valid rows intact.
- LLM classification and mapping proposals, treated as inert data. Arithmetic and ranking
  are deterministic code.
- Catalogue matching and comparison, grouped per product and per currency and unit.
- Dashboard: Gmail status, suppliers and sender rules, mapping review, product-match
  confirmation, and reports with their audit trail (sender, received time, file, worksheet,
  row). Configuration is owner-only, enforced by the API.
- Pub/Sub mode only: daily watch renewal and cursor reconciliation
  (`renew_gmail_watches_task`, 03:00 UTC). Polling mode does not schedule it.

Not delivered: see §8.

## 2. Configuration

The OAuth and encryption settings are blank by default, and the feature stays off until
they are set. The three Pub/Sub settings are needed only when `GMAIL_SYNC_MODE=pubsub`.
`.env.example` lists them with a short explanation of each:

| Variable | What it is |
|---|---|
| `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET` | OAuth client of type *Web application*. |
| `GMAIL_OAUTH_REDIRECT_URI` | `https://<api>/api/v1/gmail/oauth/callback`. It must match the client's authorized redirect URI exactly. |
| `GMAIL_WEB_REDIRECT_URI` | `https://<app>/dashboard/supplier-quotes`. The callback lands the browser here with `?gmail=connected` or `?gmail=error&reason=…`. |
| `GMAIL_TOKEN_ENCRYPTION_KEY` | 32 random bytes, base64. There is no plaintext fallback. Rotating the key makes stored tokens unreadable, and every owner must reconnect. |
| `GMAIL_SYNC_MODE` | `polling` (default) or `pubsub`. See §2.1. |
| `GMAIL_POLL_INTERVAL_MINUTES` | Polling mode: minutes between checks, default `5`. Must divide 60. |
| `GMAIL_PUBSUB_TOPIC` (pubsub only) | `projects/<project>/topics/<topic>`. |
| `GMAIL_PUBSUB_PUSH_AUDIENCE` (pubsub only) | The audience configured on the push subscription. Usually the webhook URL itself. |
| `GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT` (pubsub only) | The service account the push subscription authenticates as. |

Tunables with safe defaults: `GMAIL_ATTACHMENT_MAX_BYTES` (20 MiB),
`GMAIL_WORKBOOK_MAX_ROWS` (25,000) and `GMAIL_SALE_PRICE_MAX_AGE_DAYS` (90).

### 2.1 Choosing a sync mode

- **`polling`** (default). Each connected mailbox's history is read every
  `GMAIL_POLL_INTERVAL_MINUTES`. New mail reaches the dashboard within one interval plus
  processing time. Connecting takes the starting cursor from the Gmail profile and creates
  no watch. A mailbox in `needs_reauth` is not polled until its owner reconnects. A mailbox
  that another job is still syncing is skipped for that interval.
- **`pubsub`**. Gmail pushes a notification for each change, so mail arrives within
  seconds. It needs the Google Cloud setup in §3 and a public HTTPS webhook.

**Upgrading:** polling became the default on 2026-10-09. A deployment that already runs
Pub/Sub must set `GMAIL_SYNC_MODE=pubsub` to keep it.

**Switching modes:**

- `pubsub` → `polling`: existing watches expire on their own within 7 days. Pushes that
  arrive until then queue the same idempotent sync and do no harm.
- `polling` → `pubsub`: connections have no watch until the next 03:00 UTC renewal, so no
  push arrives for up to a day. Reconnect Gmail to start a watch immediately.

## 3. Google Cloud setup

Only for `GMAIL_SYNC_MODE=pubsub`. In polling mode, do steps 1 (Gmail API only), 6, 7
and 8, and skip the Pub/Sub topic and subscription.

The operator does this once per deployment. The application creates no cloud resources.

1. **Enable the APIs.** Turn on the *Gmail API* and *Cloud Pub/Sub API* in the project.
2. **Create the topic,** for example `gmail-supplier-quotes`, and set `GMAIL_PUBSUB_TOPIC`.
3. **Let Gmail publish to it.** Grant `roles/pubsub.publisher` on the topic to
   `gmail-api-push@system.gserviceaccount.com`. Without this grant, `users.watch` fails
   and the connect flow ends with `reason=gmail_notifications_unavailable`.
4. **Create a service account for the push,** for example `gmail-push@<project>.iam.gserviceaccount.com`,
   and set `GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT` to its address.
5. **Create the push subscription** on the topic:
   - Endpoint: `https://<api>/api/v1/gmail/pubsub`. It must be public HTTPS.
   - Enable authentication with the service account from step 4.
   - Audience: set it explicitly, and put the same value in `GMAIL_PUBSUB_PUSH_AUDIENCE`.
6. **Configure the OAuth consent screen.** Add the scope
   `https://www.googleapis.com/auth/gmail.readonly`. It is a *restricted* scope:
   - While the app is in *Testing*, only listed test users can connect, and Google expires
     their refresh tokens after 7 days. Expect reconnects.
   - Publishing to production requires Google's verification, including a security
     assessment for restricted scopes. Plan for it before onboarding real customers.
7. **Create the OAuth client** (type *Web application*). Add the callback URL from §2 as an
   authorized redirect URI, then set the client ID and secret.
8. **Generate the encryption key:**
   `python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.

## 4. Using it

1. **Connect Gmail.** An owner opens *Supplier quotes* and clicks *Connect Gmail*.
   `POST /api/v1/gmail/oauth/start` returns Google's consent URL, and the page navigates
   to it. The route returns JSON rather than redirecting because it needs the Bearer token,
   which a navigation cannot carry. Only mail that arrives after connecting is processed.
   There is no historical backfill.
2. **Add suppliers** with the sender addresses or domains their quotes come from. Mail from
   anyone else is ignored before any attachment is fetched.
3. **Review new layouts.** The first quote with an unfamiliar layout produces a *mapping
   proposal*: the worksheet, the header row, the column labels, and a suggested column for
   each field. Only labels are stored, never cell values. The owner corrects the
   suggestion and confirms it, after moving it to another worksheet or header row if needed.
   The proposal lists every header-like row in the file, by label.
   `POST /api/v1/gmail/supplier-mappings/{id}/confirm` commits the confirmation and only
   then queues the email's re-fetch, so the worker never reads an uncommitted mapping. If
   the queue is down, the confirmation still stands, and
   `POST /api/v1/gmail/supplier-messages/{id}/reprocess` queues it again. Later quotes with
   the same layout parse without review.
4. **Confirm product matches.** A row whose catalogue product was only suggested (an exact
   name match, or an LLM suggestion) waits for the owner. Confirming it ranks the quote and
   remembers the supplier's SKU for that product (`suppliers.sku_aliases`), so the next quote
   with that SKU matches without review. Each message makes at most 25 LLM match calls;
   rows beyond that wait for review.
5. **Read reports.** Each processed email gets a report. A quote row shows its gross unit
   profit (before shipping, duties, taxes and selling fees) only when the API could compute it. Otherwise the row names the reason (for example
   *Missing sale price* or *Currency differs from catalogue*) and is ranked by purchase
   price within its currency and unit group.

## 5. Comparison rules

- Offers are compared only within one catalogue product, and within one currency and
  purchase unit. Different currencies or units are never converted or ranked together.
- Profit requires a catalogue sale price that is present, valid, in the same currency and
  unit, and no older than 90 days (`Product.price_updated_at`, which changes only when the
  price or currency changes). Without one, profit is unknown and offers rank by purchase
  price.
- A price that depends on quantity tiers, with no common order quantity, is marked
  incomplete rather than guessed.
- Only each supplier's latest quote is ranked, per product, currency and unit, judged by
  the email's received time. Older quotes are marked `superseded` and leave the ranking.
- A report's best profit comes from its own email's quotes, and only when they share one
  currency. That currency is stored and shown with it.
- Profit needs the catalogue product's sale unit, in `attributes.sale_unit` (or
  `attributes.unit`). Without it, the product's offers are not comparable.

## 6. Security notes

- **Tokens.** Refresh tokens are stored only as ciphertext. Access tokens are never
  stored, and neither kind ever reaches the browser or GraphQL.
- **Content.** Email bodies and attachment bytes are never persisted or logged. Mapping
  proposals store column labels only.
- **Pre-tenant lookups.** Pub/Sub carries a mailbox address, not a tenant. The mailbox is
  resolved through `resolve_gmail_mailboxes(email)`, a restricted `SECURITY DEFINER`
  function that returns only organization and connection IDs. The worker then loads
  everything else inside `tenant_session`. The daily maintenance job uses the equally
  narrow `list_gmail_connections_for_maintenance()`.
- **Prompt injection.** Email and spreadsheet text reach the LLM only as quoted data for
  classification and mapping proposals. No tool is available to those calls.

## 7. Local development and testing

Nothing here needs a Google account:

- All Google HTTP calls in the tests go through `httpx.MockTransport` (`test_gmail_client.py`,
  `test_gmail_oauth.py`, `test_gmail_sync.py`). `test_gmail_pubsub.py` stubs Google's
  OIDC verifier and checks every claim the webhook enforces.
- Database-backed tests (`test_supplier_quote_persistence.py`,
  `test_supplier_quote_service.py`, `test_gmail_webhook.py`,
  `test_graphql_supplier_quotes.py`) need Postgres and Redis. Run them with the usual
  `localhost` overrides from the README.

A real end-to-end run needs a Google OAuth client (§3, steps 6–8). In polling mode that is
all: run the API and the worker locally, connect Gmail, and new mail is picked up within
one interval. Pub/Sub mode also needs a public HTTPS URL for the push endpoint. Pub/Sub
cannot push to `localhost`, so use a tunnel or a deployed API.

Polling is covered by `test_gmail_polling.py` (unit: job IDs and the cron task;
integration: which mailboxes are polled, at most one poll in flight per mailbox, and a
real arq worker freeing the job ID after a poll ends) and by the poll cases in
`test_gmail_pipeline.py`.

## 8. Not delivered

- **Delivering reports** outside the dashboard (email, Slack). Report generation is kept
  separate from delivery so that a channel can be added later.
- **Historical backfill** of mail received before connecting.
- **Formats other than `.xlsx`:** `.xls`, `.csv` and PDF quotes are skipped.
- **Currency or unit conversion** between offers.
- **More than one mailbox** per organization.
- **Live verification against Google.** The integration is covered by mocked-transport and
  database tests only. No live Gmail, Pub/Sub or OAuth approval was exercised while
  building it.
