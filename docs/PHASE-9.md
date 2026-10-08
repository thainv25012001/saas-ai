# Phase 9 — Gmail supplier quote analysis

> Built from [the design](superpowers/specs/2026-10-08-gmail-supplier-quote-analysis-design.md)
> and [the plan](superpowers/plans/2026-10-08-gmail-supplier-quotes.md). This document
> records what was delivered, how to configure it, and where it departs from the design.

**Goal:** when a supplier emails an `.xlsx` quote to the organization's Gmail, the SaaS
reads it, normalizes whatever layout the supplier uses, compares each offer against the
catalogue, and shows the result in the dashboard. No one forwards or re-types anything.

```text
Gmail (watch) ──publish──▶ Pub/Sub topic ──authenticated push──▶ POST /api/v1/gmail/pubsub
                                                                       │ verify OIDC token,
                                                                       │ resolve mailbox → tenant
                                                                       ▼
                                                     arq: sync_gmail_mailbox_task
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
  Owners connect and disconnect, and disconnecting stops the watch and deletes the stored
  credentials.
- Pub/Sub push webhook. Every push is verified (signature, issuer, audience, service
  account and verified email) before any database lookup happens.
- Mailbox sync from the watch cursor onward. Pub/Sub redelivery and overlapping history
  pages never duplicate a message or a quote row.
- Workbook inspection and parsing (`openpyxl`, read-only and data-only, so formulas are
  never evaluated), with row-level issues that leave valid rows intact.
- LLM classification and mapping proposals, treated as inert data. Arithmetic and ranking
  are deterministic code.
- Catalogue matching and comparison, grouped per product and per currency and unit.
- Dashboard: Gmail status, suppliers and sender rules, mapping review, and reports.
- Daily watch renewal and cursor reconciliation (`renew_gmail_watches_task`, 03:00 UTC).

Not delivered: see §8.

## 2. Configuration

Every setting is blank by default, and the feature stays off until all of them are set.
`.env.example` lists them with a short explanation of each:

| Variable | What it is |
|---|---|
| `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET` | OAuth client of type *Web application*. |
| `GMAIL_OAUTH_REDIRECT_URI` | `https://<api>/api/v1/gmail/oauth/callback`. It must match the client's authorized redirect URI exactly. |
| `GMAIL_WEB_REDIRECT_URI` | `https://<app>/dashboard/supplier-quotes`. The callback lands the browser here with `?gmail=connected` or `?gmail=error&reason=…`. |
| `GMAIL_TOKEN_ENCRYPTION_KEY` | 32 random bytes, base64. There is no plaintext fallback. Rotating the key makes stored tokens unreadable, and every owner must reconnect. |
| `GMAIL_PUBSUB_TOPIC` | `projects/<project>/topics/<topic>`. |
| `GMAIL_PUBSUB_PUSH_AUDIENCE` | The audience configured on the push subscription. Usually the webhook URL itself. |
| `GMAIL_PUBSUB_EXPECTED_SERVICE_ACCOUNT` | The service account the push subscription authenticates as. |

Tunables with safe defaults: `GMAIL_ATTACHMENT_MAX_BYTES` (20 MiB),
`GMAIL_WORKBOOK_MAX_ROWS` (25,000) and `GMAIL_SALE_PRICE_MAX_AGE_DAYS` (90).

## 3. Google Cloud setup

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
   suggestion and confirms it. Confirming commits first, then the page calls
   `POST /api/v1/gmail/supplier-messages/{id}/reprocess`, so the worker never reads an
   uncommitted mapping. Later quotes with the same layout parse without review.
4. **Read reports.** Each processed email gets a report. A quote row shows its unit profit
   only when the API could compute it. Otherwise the row names the reason (for example
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

A real end-to-end run needs a real Google project (§3) and a public HTTPS URL for the
push endpoint. Pub/Sub cannot push to `localhost`, so use a tunnel or a deployed API.

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
