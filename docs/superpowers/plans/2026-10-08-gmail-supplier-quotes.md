# Gmail Supplier Quote Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Process new Gmail messages, recognize supplier quotes, normalize varied XLSX layouts, compare supplier offers, and show reports in the SaaS dashboard.

**Architecture:** Extend the existing FastAPI modular monolith, PostgreSQL/RLS, Redis/arq workers, LLM provider interface, GraphQL dashboard API, and Next.js dashboard. Gmail OAuth and Pub/Sub are boundary adapters; parsing and quote comparison remain provider-independent domain code. Persist normalized quote data, mapping proposals, and source identifiers, not raw email bodies, sample cell values, or attachment bytes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic, `openpyxl`, `httpx`, `google-auth`, arq/Redis, Strawberry GraphQL, Next.js/TypeScript, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-gmail-supplier-quote-analysis-design.md`

## Global Constraints

- Process new mail from the Gmail watch cursor onward; historical backfill is not part of the default flow.
- Support `.xlsx` attachments only in the first release; never execute spreadsheet formulas or macros.
- If the matched product has no sale price, profit stays unknown and comparable offers rank by purchase price.
- Persist reports and show them in the SaaS dashboard; do not send them to an external destination.
- All tenant-owned records use the existing `TenantMixin`, `tenant_session`, explicit organization filters, and PostgreSQL RLS.
- The LLM may classify, extract, and propose mappings; deterministic application code performs currency/unit comparability checks and arithmetic.
- Do not silently compare offers across currencies or purchase units; do not log email bodies, workbook contents, tokens, or extracted commercial data.
- OAuth refresh tokens are encrypted at rest; Pub/Sub push requests are authenticated by signature, issuer, audience, and configured service-account identity.
- A catalogue sale price older than 90 days or without a known observation time is treated as stale for profit calculation and remains visible with that status.

## Review Focus

- Multi-sheet workbooks, repeated header rows, merged cells, changed columns, and formula cells without cached results must be reviewable rather than misparsed (Task 1).
- Pub/Sub redelivery and overlapping Gmail history pages must not duplicate supplier messages or quotes (Tasks 3 and 6).
- Mailbox lookup from an unauthenticated tenant context must resolve only minimal IDs through a restricted database function; processing must then enter the correct tenant session (Tasks 3 and 6).
- Missing sale price, stale/absent catalog price, currency mismatch, and package-unit mismatch must never produce a misleading profit ranking (Task 2).
- Prompt injection in email text or XLSX cells must be treated as data and cannot authorize tools or external actions (Tasks 1 and 6).

## Implementation assumptions for review

- A catalogue sale price is considered fresh for 90 days. Existing priced products get `price_updated_at` backfilled from their current `updated_at`; future writes track actual price/currency changes. The original spec requires stale-price handling but does not set this cutoff.
- The operator will configure the Google OAuth client, Pub/Sub topic, and push service-account identity; this implementation adds the application-side integration and setup guide, not resources in a Google Cloud project.
- LLM classification and suggestions use the configured application default provider/model. With the offline `fake` provider or an unavailable LLM, uncertain cases remain in the dashboard review queue.

---

### Task 1: Canonical supplier quote rows and workbook mapping parser

**Files:**
- Create: `apps/api/app/supplier_quotes/__init__.py`
- Create: `apps/api/app/supplier_quotes/schemas.py`
- Create: `apps/api/app/supplier_quotes/workbooks.py`
- Test: `apps/api/tests/unit/test_supplier_quote_workbooks.py`

**Interfaces:**
- Produces `WorkbookMapping`, `WorkbookInspection`, `CanonicalQuoteRow`, `WorkbookRowIssue`, and `WorkbookParseResult` Pydantic models.
- Produces `inspect_supplier_workbook(data: bytes) -> WorkbookInspection`, which returns sheet names, candidate header rows, column labels, and at most five sample rows for an in-memory mapping suggestion; sample cell values are not persisted.
- Produces `parse_supplier_workbook(data: bytes, mapping: WorkbookMapping) -> WorkbookParseResult`.
- `WorkbookMapping` identifies a sheet, header row, source-column names, and explicit per-supplier defaults for currency and purchase unit.
- `CanonicalQuoteRow` retains source sheet/row, supplier SKU, product name/specification, decimal unit price, currency, unit, MOQ, quantity tiers, lead time, validity, and extraction confidence.

- [x] **Step 1: Write failing unit tests** for inspection of a multi-sheet workbook, a non-first header row, and a five-row sample cap; also test parsing with mapped column synonyms, blank optional MOQ, and exact source row metadata.
- [x] **Step 2: Run the focused tests** with `cd apps/api && uv run pytest tests/unit/test_supplier_quote_workbooks.py -q`; confirm failure because the module and parser do not exist.
- [x] **Step 3: Implement the inspection and parser.** Load `.xlsx` bytes with `openpyxl` in read-only/data-only mode; inspection returns bounded labels/sample values for an in-memory LLM mapping suggestion, while parsing uses only a confirmed profile, parses price with `Decimal`, and produces row-level issues without failing unrelated valid rows.
- [x] **Step 4: Add failing-then-passing tests** for missing worksheets, invalid price, repeated headers, merged cells, missing cached formula value, an invalid ZIP/XLSX payload, a file over 20 MiB, and a worksheet over 25,000 rows. Unsupported or ambiguous workbook structure returns a bounded parse issue; formulas are never evaluated.
- [x] **Step 5: Run `cd apps/api && uv run pytest tests/unit/test_supplier_quote_workbooks.py -q`** and verify all focused cases pass.

### Task 2: Supplier offer matching and deterministic comparison

**Files:**
- Create: `apps/api/app/supplier_quotes/analysis.py`
- Create: `apps/api/tests/unit/test_supplier_quote_analysis.py`

**Interfaces:**
- Produces `OfferComparison`, with `profit_status`, nullable `unit_gross_profit`, nullable margin, `ranking_basis`, comparability reasons, and sale-price source timestamp.
- Produces `compare_product_offers(offers, catalog_product, now) -> OfferComparison`; a sale price is usable only when `price_updated_at` exists and is no more than 90 days old.
- Produces `match_catalog_product(quote, products, confirmed_supplier_sku_map) -> MatchResult`; exact catalogue SKU and confirmed supplier mappings take precedence over suggestions.

- [x] **Step 1: Write failing tests** for known sale price, missing sale price, missing quote price, currency mismatch, unit mismatch, and offers grouped by product rather than ranked across unrelated products.
- [x] **Step 2: Run `cd apps/api && uv run pytest tests/unit/test_supplier_quote_analysis.py -q`** and confirm the new comparison functions are missing.
- [x] **Step 3: Implement pure comparison functions.** Use `Decimal`; compute unit gross profit only for compatible units/currency with an available catalog price. Otherwise set profit to unknown and return a precise reason. When sale price is absent, rank only comparable purchase prices.
- [x] **Step 4: Add failing-then-passing tests** for zero/negative prices, ties, absent catalog price, prices older than 90 days, and quantity-tier offers where a common order quantity is not supplied. Do not infer a quantity tier; surface those cases as incomplete.
- [x] **Step 5: Run `cd apps/api && uv run pytest tests/unit/test_supplier_quote_analysis.py -q`** and confirm deterministic ranking.

### Task 3: Tenant-scoped persistence and Gmail event resolution

**Files:**
- Create: `apps/api/app/db/models/gmail_connection.py`
- Create: `apps/api/app/db/models/supplier.py`
- Create: `apps/api/app/db/models/supplier_quote.py`
- Modify: `apps/api/app/db/models/product.py`
- Modify: `apps/api/app/products/service.py`
- Modify: `apps/api/tests/integration/test_product_service.py`
- Modify: `apps/api/app/db/models/__init__.py`
- Create: `apps/api/alembic/versions/0017_supplier_quotes.py`
- Create: `apps/api/tests/integration/test_supplier_quote_persistence.py`
- Create: `apps/api/tests/unit/test_supplier_quote_models.py`

**Interfaces:**
- Models: `GmailConnection`, `Supplier`, `SupplierMapping`, `SupplierMessage`, `SupplierQuote`, and `SupplierReport`.
- Every tenant-owned table has `organization_id`, an index, explicit foreign-key delete behavior, and RLS enabled in the migration.
- Add nullable `Product.price_updated_at`; migration backfills it from `updated_at` for existing products with a price. `ProductService.upsert_many` initializes it for a priced insert and changes it only when price or currency changes.
- OAuth token fields store ciphertext only. Message identity is unique per mailbox; quote identity is unique per message, attachment, sheet, and source row.
- Migration function `resolve_gmail_mailboxes(email)` returns only matching `(organization_id, connection_id)` pairs and is executable only by the configured runtime role. Tenant-scoped workers reload each record in `tenant_session` before reading credentials or touching provider data.

- [x] **Step 1: Write failing unit tests** for uniqueness, nullable sale-price behavior in stored quote/report projections, and ciphertext-only model fields.
- [x] **Step 2: Run `cd apps/api && uv run pytest tests/unit/test_supplier_quote_models.py -q`** and verify the expected missing-model failures.
- [x] **Step 3: Add the ORM models and Alembic migration.** Register models for metadata, create explicit indexes/constraints, call `enable_rls` for tenant-owned tables, and create/restrict the minimal mailbox resolver function following the `resolve_widget` migration pattern.
- [x] **Step 4: Add integration tests** proving tenant A cannot read tenant B's suppliers, mappings, messages, quotes, or reports; the pre-tenant resolver exposes only mailbox and organization IDs for an exact connected address; and `price_updated_at` changes only when price or currency changes.
- [x] **Step 5: Run focused unit and integration tests**, including `cd apps/api && uv run pytest tests/unit/test_supplier_quote_models.py tests/integration/test_supplier_quote_persistence.py -q`.

### Task 4: Supplier services, mapping confirmation, and report queries

**Files:**
- Create: `apps/api/app/supplier_quotes/service.py`
- Create: `apps/api/app/supplier_quotes/schemas.py` updates only for API inputs/outputs, keeping workbook domain schemas stable.
- Create: `apps/api/tests/integration/test_supplier_quote_service.py`

**Interfaces:**
- `SupplierQuoteService(session, tenant)` supports create/list/update supplier, save/version workbook mappings, record processing status, upsert normalized quote rows idempotently, and list report summaries/details.
- All methods include explicit organization predicates in addition to relying on RLS.
- A mapping update creates a new version; prior quote rows retain the version used to parse them.
- When a mapping needs user review, persist only column labels and the proposed field mapping, not sample cell values or attachment bytes. After confirmation, refetch the attachment from Gmail using the saved message/attachment identifiers and process it with the confirmed mapping.

- [x] **Step 1: Write failing integration tests** for supplier CRUD, mapping version increments, idempotent quote upsert, report retrieval, and cross-tenant not-found behavior.
- [x] **Step 2: Run `cd apps/api && uv run pytest tests/integration/test_supplier_quote_service.py -q`** and confirm failures are due to absent service behavior.
- [x] **Step 3: Implement the tenant-bound service** with explicit organization filters, one transaction per cohesive write, and bounded status/error fields that never persist raw email bodies.
- [x] **Step 4: Add a test** proving a failed row can be recorded for review while valid rows in the same attachment remain persisted.
- [x] **Step 5: Run the focused service integration tests** and verify database cleanup leaves no rows for either tenant.

### Task 5: OAuth, encrypted credentials, and Gmail REST client

**Files:**
- Create: `apps/api/app/gmail/__init__.py`
- Create: `apps/api/app/gmail/oauth.py`
- Create: `apps/api/app/gmail/client.py`
- Create: `apps/api/app/gmail/token_crypto.py`
- Modify: `apps/api/app/core/config.py`
- Modify: `apps/api/pyproject.toml` and `apps/api/uv.lock`
- Create: `apps/api/tests/unit/test_gmail_oauth.py`
- Create: `apps/api/tests/unit/test_gmail_client.py`
- Create: `apps/api/tests/unit/test_gmail_token_crypto.py`

**Interfaces:**
- Settings include Gmail OAuth client ID/secret, callback URL, token-encryption key, Pub/Sub topic, push audience, and expected push service-account email; all are optional at startup so the rest of the app remains usable without Gmail configured.
- Settings default XLSX attachment size to 20 MiB, workbook rows to 25,000 per sheet, preview rows to five, and maximum sale-price age to 90 days.
- `encrypt_token(plaintext: str) -> str` and `decrypt_token(ciphertext: str) -> str` use authenticated encryption and reject wrong keys/tampering.
- `GmailClient` uses `httpx` for OAuth exchange/refresh and Gmail REST calls: profile, watch/stop, history pages, full message metadata/body parts, and attachment bytes.
- OAuth authorization is server-side, state is one-time and bound to the authenticated organization/user, scopes are limited to `gmail.readonly`, and access tokens are never sent to the browser.

- [x] **Step 1: Write failing unit tests** for token encryption round-trip, wrong-key/tamper rejection, OAuth state expiry/replay, exact read-only scope construction, and token refresh preserving a previously stored refresh token when Google omits a new one.
- [x] **Step 2: Run the three focused test files** and confirm the implementation is absent.
- [x] **Step 3: Add direct runtime dependencies `cryptography>=44`, `httpx>=0.27`, and `google-auth[requests]>=2`** to `apps/api/pyproject.toml`, then update `apps/api/uv.lock`. The existing `httpx` declaration is dev-only. Implement AES-GCM encryption with a configured 256-bit key; never fall back to plaintext if the key is missing.
- [x] **Step 4: Implement OAuth state/PKCE and Gmail client calls.** Validate callback state before token exchange, fetch the connected mailbox identity, persist refresh-token ciphertext, refresh access tokens server-side, and convert provider errors to bounded application errors.
- [x] **Step 5: Run the focused Gmail unit tests** with HTTP requests intercepted by `httpx.MockTransport`; assert scopes, endpoint paths, payloads, and no token/body values in raised errors.

### Task 6: Pub/Sub webhook, mailbox sync, and queued XLSX processing

**Files:**
- Create: `apps/api/app/gmail/pubsub.py`
- Create: `apps/api/app/gmail/sync.py`
- Create: `apps/api/app/supplier_quotes/classification.py`
- Create: `apps/api/app/api/gmail.py`
- Create: `apps/api/app/api/pubsub.py`
- Modify: `apps/api/app/main.py`
- Modify: `apps/api/app/workers/tasks.py`
- Modify: `apps/api/app/workers/settings.py`
- Create: `apps/api/tests/unit/test_gmail_pubsub.py`
- Create: `apps/api/tests/unit/test_gmail_sync.py`
- Create: `apps/api/tests/unit/test_supplier_classification.py`
- Create: `apps/api/tests/integration/test_gmail_webhook.py`

**Interfaces:**
- OAuth REST routes start connection, complete callback, report connection status, and disconnect; the public Pub/Sub route is separate from user-authenticated routes.
- `verify_pubsub_request(authorization, expected_audience, expected_email)` verifies Google JWT signature and claims before decoding or queuing payload data.
- `gmail_push` enqueues `sync_gmail_mailbox_task` with the mailbox email/history ID and acknowledges only after the message is durably queued or recognized as a duplicate.
- `sync_gmail_mailbox_task` resolves mailbox IDs through the restricted function, enters each tenant session, calls `history.list` from the saved cursor, and processes each new INBOX message once.
- Classification and product-match suggestions use configured sender/domain and confirmed SKU rules first, then the existing `LLMProvider.generate_structured` interface with the configured default provider/model for unknown cases. Workbook mapping suggestions use only the bounded in-memory inspection from Task 1. LLM output is validated and cannot invoke tools. Low-confidence classification, mapping, or product match is saved for review.
- A mapping review stores its message/attachment identifiers and proposed column mapping only; the confirm mutation queues a job that refetches the attachment from Gmail. No attachment bytes or sampled cell values are persisted.
- Worker renewal and reconciliation cron jobs renew Gmail watches daily and run a bounded history sync. A stale history cursor triggers a safe reconciliation using the last successful sync timestamp and idempotent message IDs.

- [x] **Step 1: Write failing unit tests** for valid/invalid Pub/Sub claims, malformed base64 payloads, duplicate/out-of-order mailbox history events, Gmail history pagination, messages with multiple attachments, sender rules, structured LLM results, low-confidence classification/mapping/product match outcomes, and email/XLSX prompt-injection text being treated as inert data.
- [x] **Step 2: Run the focused tests** and verify failures reflect missing handler/client behavior.
- [x] **Step 3: Implement Pub/Sub verification and webhook.** Verify signature, issuer, configured audience, service-account email, and verified-email claim. Reject invalid requests before DB resolution. Decode only the Pub/Sub envelope and enqueue work; do not log its contents.
- [x] **Step 4: Implement sync and worker tasks.** Fetch history pages, serialize cursor advancement per mailbox, advance the saved cursor only after each batch is durably processed, filter to newly added inbox messages, fetch XLSX attachments, classify sender, suggest/confirm mappings, match products, and persist rows/issues/report. Register each job and renewal/reconciliation cron in `WorkerSettings`.
- [x] **Step 5: Add and run a tenant integration test** where a valid push for one mailbox cannot enqueue or fetch another tenant's credentials, plus an unauthenticated/incorrect-audience request test that never reaches the resolver.
- [x] **Step 6: Run `cd apps/api && uv run pytest tests/unit/test_gmail_pubsub.py tests/unit/test_gmail_sync.py tests/unit/test_supplier_classification.py tests/integration/test_gmail_webhook.py -q`** and `uv run pytest tests/unit/test_worker_settings.py -q`.

### Task 7: GraphQL supplier setup, mappings, and reports

**Files:**
- Modify: `apps/api/app/graphql/types.py`
- Modify: `apps/api/app/graphql/resolvers.py`
- Modify: `apps/api/app/graphql/context.py` only if a service DataLoader is required.
- Modify: `apps/web/src/graphql/operations.graphql`
- Modify: `packages/shared/schema.graphql` via `make schema`
- Modify: `apps/web/src/graphql/generated.ts` via `npm run codegen`
- Create: `apps/api/tests/integration/test_graphql_supplier_quotes.py`

**Interfaces:**
- GraphQL queries expose Gmail connection status, suppliers, pending mapping/review items, quote reports, and report details.
- Mutations create/update supplier sender rules and confirm a proposed mapping version. OAuth token values, raw email bodies, and attachment bytes are never GraphQL fields.
- All resolvers use `Context`, `_require_tenant`, and `SupplierQuoteService` following existing product resolvers.

- [x] **Step 1: Write failing GraphQL integration tests** for authenticated tenant-only lists, unauthenticated rejection, supplier create/update, mapping confirmation, report detail, and absence of secret/content fields in the schema.
- [x] **Step 2: Run `cd apps/api && uv run pytest tests/integration/test_graphql_supplier_quotes.py -q`** and confirm the fields/mutations are not present.
- [x] **Step 3: Implement Strawberry types and resolvers** with explicit pagination limits and ownership checks.
- [x] **Step 4: Add operations to `operations.graphql`, including mapping-confirmation requeue; run `make schema` then `cd apps/web && npm run codegen`, and add an assertion test for generated query use only through typed documents.
- [x] **Step 5: Run the focused GraphQL integration test and `cd apps/web && npm run typecheck`**.

### Task 8: Supplier quote dashboard and mapping review

**Files:**
- Modify: `apps/web/src/components/shell/nav.ts`
- Create: `apps/web/src/app/dashboard/supplier-quotes/page.tsx`
- Create: `apps/web/src/components/supplier-quotes/SupplierList.tsx`
- Create: `apps/web/src/components/supplier-quotes/MappingReview.tsx`
- Create: `apps/web/src/components/supplier-quotes/QuoteReports.tsx`
- Create: `apps/web/src/lib/supplier-quotes.ts`
- Create: component/library tests adjacent to each new component.

**Interfaces:**
- Dashboard page connects/disconnects Gmail via REST OAuth endpoints, edits supplier sender rules, lets the user edit and confirm proposed worksheet/header/column mappings, and lists report summaries/details. Mapping previews show header labels and proposed mappings; they do not persist or display sampled cell values.
- Report rows show profit only when computable; otherwise display “Missing sale price” and the purchase-price rank. Incomparable currency/unit rows are labeled and not placed in the same ranking.
- No outbound report delivery controls or destination selection are exposed.

- [x] **Step 1: Write failing Vitest tests** for mapping preview confirmation, missing-price rendering, purchase-price rank, unknown currency/unit status, loading/error/empty states, and no send-destination UI.
- [x] **Step 2: Run the focused Vitest files** and confirm missing component/formatting behavior.
- [x] **Step 3: Implement the dashboard using generated GraphQL documents, the authenticated REST client, and existing page/card/table patterns.** Show proposed mappings with header labels, permit mapping edits before confirmation, and avoid rendering email bodies, sample cell values, or entire workbooks.
- [x] **Step 4: Run focused Vitest tests, then `cd apps/web && npm run typecheck && npm run lint`**.

### Task 9: Configuration and deployment documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/PHASE-9.md`

- [x] **Step 1: Add blank settings to `.env.example`** for Google OAuth client ID/secret, OAuth redirect URL, web dashboard redirect URL, AES key, Pub/Sub topic, push audience, and expected push service-account email. Keep the existing LLM defaults and keys unchanged.
- [x] **Step 2: Document Google Cloud setup**: enable Gmail API and Pub/Sub, create the topic, grant Gmail publish permission, configure authenticated push subscription audience/service-account identity, configure OAuth consent/scopes/redirect URI, and generate the encryption key.
- [x] **Step 3: Document local development with fake Gmail/Pub/Sub transport fixtures**, the environment variables, OAuth verification considerations, watch renewal/reconciliation behavior, mapping confirmation, and report access in the dashboard.
- [x] **Step 4: Link `docs/PHASE-9.md` from `README.md`, then run `git diff --check`**.

### Task 10: Full verification and branch review

**Files:** all files in Tasks 1–9.

- [x] **Step 1: Run the complete API suite:** `make test`.
- [x] **Step 2: Run API checks:** `make lint`.
- [x] **Step 3: Run complete web tests, types, and lint:** `cd apps/web && npm test && npm run typecheck && npm run lint`.
- [x] **Step 4: Run `git diff --check`, inspect the complete diff against the spec, and verify migration upgrade/downgrade and generated GraphQL artifacts are included.**
- [x] **Step 5: Record any unavailable live-Google verification explicitly.** No Google OAuth credentials, Cloud Pub/Sub topic, or external OAuth approval may be fabricated; unit/integration tests must cover the local behavior.
