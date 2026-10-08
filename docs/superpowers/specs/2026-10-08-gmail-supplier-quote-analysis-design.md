# Gmail Supplier Quote Analysis

**Status:** Draft for user review  
**Date:** 2026-10-08

## 1. Goal

When a new email arrives in a connected Gmail account, the SaaS should determine
whether it came from a supplier, extract product offers from XLSX attachments even
when suppliers use different spreadsheet layouts, compare offers, and create a
report for the account owner.

The system must not invent sale prices or profit. If a sale price is available for
the matched product, it may calculate a clearly labeled unit gross profit from the
known values. If no sale price is available, profit remains unknown and the offer
is ranked by comparable purchase price.

## 2. Existing project context

The repository is already a multi-tenant SaaS with a FastAPI modular monolith,
Next.js dashboard, PostgreSQL with row-level tenant isolation, Redis/arq background
workers, an LLM provider abstraction, product records, and XLSX catalogue imports
using `openpyxl`. The feature should extend these patterns. It should not introduce
a separate microservice or replace the existing product catalogue importer.

Supplier quotes are a distinct domain from catalogue imports: the same product can
have many offers from different suppliers, each tied to an email and source file.

## 3. User-facing workflow

1. An organization owner connects Gmail through OAuth.
2. The owner configures known suppliers and reviews suggested spreadsheet mappings
   for each supplier.
3. Gmail notifies the backend of mailbox changes. A worker fetches newly arrived
   messages and relevant XLSX attachments.
4. The system classifies each email. Non-supplier messages are marked ignored;
   uncertain classifications are surfaced for review.
5. For a known supplier, the saved workbook mapping is applied. For a new supplier
   or changed layout, the system proposes a mapping and waits for confirmation when
   confidence is insufficient.
6. Valid quote rows are normalized, matched to products, compared, and persisted.
7. A report is created and shown in the SaaS dashboard. External delivery is out of
   scope until the user chooses a destination.

## 4. Architecture

```text
Gmail watch
    -> Google Cloud Pub/Sub
    -> FastAPI notification endpoint
    -> Redis/arq job
    -> Gmail history/message/attachment fetch
    -> supplier classification
    -> supplier-specific workbook mapping
    -> canonical quote rows and product matching
    -> deterministic comparison and profit calculation
    -> PostgreSQL report and dashboard view
```

The notification endpoint should validate the Pub/Sub request, enqueue work, and
acknowledge promptly. It should not download or parse workbooks inline. Jobs must
be idempotent, keyed by organization, Gmail message, and attachment so Pub/Sub
retries cannot create duplicate quotes.

Gmail watch subscriptions expire. A scheduled worker must renew them before
expiration, and a periodic history sync must recover changes if notifications are
late or missed. The initial connection should process new mail from the watch
cursor onward; historical inbox backfill is not included in the MVP unless enabled
as a separate option.

## 5. Supplier and workbook normalization

Suppliers do not need to change their XLSX templates. Each supplier gets a versioned
mapping profile that may specify:

- Worksheet selection and header row.
- Source column to canonical field mappings.
- Row start/stop rules and any repeated header rows.
- Default currency or unit only when explicitly configured for that supplier.
- Supplier SKU aliases and product matching rules.

For an unseen supplier or layout, the system may use the LLM to suggest worksheet,
header, and column mappings. Suggestions must be validated against known fields and
shown with confidence. A user confirms a new mapping once; later files with the
same layout reuse it. Layout drift invalidates or flags the mapping rather than
silently applying a likely-wrong interpretation.

The canonical quote row should contain at least:

| Field | Meaning |
|---|---|
| `supplier_id` | Configured supplier identity |
| `supplier_sku` | Supplier's product identifier, if present |
| `product_id` | Matched catalogue product, nullable pending review |
| `product_name` / `specification` | Supplier-provided description |
| `unit_price` | Decimal purchase price per stated unit |
| `currency` / `unit` | Currency and purchase unit |
| `minimum_order_quantity` | MOQ, when present |
| `quantity_tiers` | Quantity-dependent unit prices, when present |
| `lead_time` / `valid_until` | Optional quote terms |
| `source_message_id` / `attachment_id` | Gmail source identity |
| `sheet` / `row` | Exact workbook source location |
| `mapping_version` / `confidence` | Audit and review information |

Only XLSX files are processed in the first release. Unsupported formats, missing
required values, invalid numeric prices, or ambiguous product matches are recorded
as review items. Valid rows in the same workbook may continue. Spreadsheet formulas
are not executed; formula cells without a usable cached value require review.

## 6. Supplier classification and product matching

Classification should combine configured sender addresses/domains and supplier
identity with LLM classification for unknown cases. A low-confidence result must
not be treated as a confirmed supplier quote.

Product matching should prefer exact catalogue SKU or a previously confirmed
supplier-SKU mapping. The LLM can propose a match from names and specifications,
but uncertain matches require review. Every extracted value and match must retain
its source email and workbook location.

Email bodies and attachments are untrusted input. They are data to classify and
extract, not instructions to execute or authority to invoke unrelated tools.

## 7. Purchase-price ranking and profit rules

Comparison is performed per matched product, not across unrelated products.

- Offers are directly comparable only when the product, purchase unit, and currency
  match. The MVP does not silently convert currencies or package units. When they
  differ and no explicit conversion is configured, show separate groups and flag
  the comparison as incomplete.
- When a current sale price exists in the product catalogue, calculate
  `unit_gross_profit = sale_price - unit_purchase_price` for matching units and
  currency. Label this as gross profit before shipping, duties, taxes, and selling
  fees unless those costs are explicitly supplied and included.
- When sale price is absent, set profit to unknown, show “missing sale price,” and
  rank comparable offers by lowest purchase price. Do not estimate a sale price.
- If a cost basis or price is stale or has an incompatible unit/currency, do not
  present a calculated profit as comparable. Show the missing or mismatched input.
- Reports should include the basis and source of each sale price and supplier quote.

All arithmetic and ranking are deterministic application code. The LLM extracts,
classifies, and proposes mappings; it does not calculate or invent profit values.

## 8. Report behavior

The MVP creates an in-product report record and dashboard view. It should include:

- Offers grouped by matched product.
- Lowest comparable purchase price and supplier.
- Gross profit only where the required sale price and comparable quote exist.
- Explicit missing-data and incomparable-offer statuses.
- Supplier, email date, attachment, worksheet, and source row for audit.
- Processing status and items requiring user review.

The report-delivery channel is intentionally deferred. The design should keep report
generation separate from delivery so a later decision can add email, Slack, or
another destination without changing quote extraction and comparison.

## 9. Data, security, and operations

- All supplier connections, mappings, quotes, and reports are organization-scoped
  and protected by the existing PostgreSQL RLS pattern.
- Encrypt Gmail refresh tokens at rest; support disconnecting Gmail and deleting
  stored credentials. Request only the Gmail scopes required for the approved
  workflow.
- Keep webhook handling short; use arq jobs with bounded retries, deduplication,
  observable status transitions, and a failure queue or equivalent review state.
- Enforce attachment size and type limits. Reject macro-enabled spreadsheets in
  the XLSX-only MVP.
- Do not log email bodies, attachment contents, access tokens, or extracted
  commercial data in routine logs.
- Public SaaS release must account for Google's OAuth verification and restricted
  scope requirements for Gmail read access.

## 10. MVP milestones

1. Define and validate the canonical quote schema and profit rules using a small
   set of representative supplier XLSX files.
2. Add supplier management, versioned mapping profiles, workbook parsing, review
   states, and quote persistence.
3. Add Gmail OAuth connection, Pub/Sub notifications, history synchronization,
   watch renewal, and queued processing.
4. Add quote comparison and the in-product report view.
5. Pilot with real supplier files; tune mapping confidence and review thresholds
   before enabling unattended processing broadly.

## 11. Acceptance criteria

- A new Gmail message is processed once even if its notification is retried.
- Non-supplier email is ignored or flagged according to classification confidence.
- A confirmed supplier mapping handles later XLSX files with the same layout.
- A changed or unknown layout is surfaced for confirmation instead of silently
  corrupting quote fields.
- Each normalized quote links to its source message, attachment, worksheet, and row.
- With a comparable sale price, profit is calculated by deterministic code and
  labeled with included/excluded costs.
- Without a sale price, profit remains unknown and comparable offers are ranked by
  purchase price.
- Incomparable currencies, units, or product matches are visibly flagged.
- The report is available in the SaaS dashboard; no outbound report destination is
  selected or implemented in the MVP.

## 12. Assumptions for review

- Only new messages after Gmail connection are processed by default; historical
  backfill is a separate choice.
- The first supported attachment type is `.xlsx`.
- Missing sale price means purchase-price ranking, never an AI-generated estimate.
- The MVP report is stored and displayed in the SaaS; external delivery is deferred.
- The default profit metric is unit gross profit before costs that are not supplied
  or configured.
- Cross-currency and cross-unit comparisons require explicit conversion data; no
  implicit conversion is performed.
