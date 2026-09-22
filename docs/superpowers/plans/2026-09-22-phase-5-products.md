# Phase 5 — Products as structured knowledge: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the assistant answers *what do you sell, what does it cost, is it in stock* from data rather than from prose that happens to mention a price.

**Spec:** [docs/PHASE-5.md](../../PHASE-5.md), extending [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) §3.4, §5.4 and §7.2.

## Global Constraints

- Python **3.12**, `uv`; API code under `apps/api/app/`. **GNU `make` is NOT available** — run underlying commands directly.
- Async tests use **`anyio` only**; `pytest-asyncio` is deliberately absent. Use `pytestmark = pytest.mark.anyio`.
- The suite runs with `filterwarnings = ["error"]` — any `DeprecationWarning` fails it.
- **No test may make a network call.** Use `FakeProvider`, `HashingEmbedder`, or a mocked SDK client.
- **Integration tests need all three overrides in every shell invocation.** `conftest.py` sets `DATABASE_URL` and `MIGRATION_DATABASE_URL` but **not** `REDIS_URL`, so the root `.env`'s docker hostname leaks through and produces ~146 spurious errors:
  ```sh
  export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai"
  export MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai"
  export REDIS_URL="redis://localhost:6379/0"
  ```
  Each Bash call is a fresh shell. Start infra with `docker compose up -d --wait db redis`. **Check no other `pytest` is running before starting one** — concurrent runs against the shared database have produced phantom failures repeatedly.
- Tenant-owned tables get `organization_id UUID NOT NULL` + RLS via `enable_rls(op, table)`. Never inline policy SQL.
- **Two-layer tenancy is mandatory** (§2.3): an explicit `organization_id` predicate *in addition to* RLS, on every query.
- **PostgreSQL FK checks bypass the referencing session's RLS.** Any INSERT establishing a new FK needs a scoped ownership SELECT first.
- Primary keys are UUIDv7 from `app.core.ids.uuid7()`. Money is `Decimal`.
- `ruff check .`, `ruff format --check .`, `mypy --strict app/` must pass.
- Conventional Commits ending with exactly:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Baseline: 774 backend tests, 258 web tests** on `main`.

## Existing interfaces you build on

- `app/embeddings/` — `get_embedding_provider()`, `EmbeddingProvider.embed(texts) -> list[list[float]]`. The default `HashingEmbedder` is real (hashed bag-of-words, 1536-dim, L2-normalised), so retrieval tests mean something offline.
- `app/rag/retrieve.py` — the hybrid pattern to follow: two candidate arms fused with RRF, `<=>` cosine *distance* (smaller is better), `websearch_to_tsquery('english', …)`, explicit `organization_id` on both arms plus RLS, a `> 0` rank floor on the strict keyword arm, and `documents.status = 'ready'`. Also `excerpt`, `EXCERPT_MAX_CHARS`, `build_citation`, `CitationPayload`.
- `app/rag/ingest.py`, `app/rag/queue.py`, `app/workers/` — the arq ingestion shape: upload → `pending` → job → `ready`/`failed`, with the failure status committed through an independent session.
- `app/documents/service.py` — the service idiom, including the scoped ownership SELECT before an FK-establishing write.
- `app/tools/base.py`, `registry.py` — `AgentTool`, `ToolContext`, `ToolResult`; the registry rejects an `args_model` exposing `organization_id` in any namespace, and turns invalid args, timeouts, unknown names and raised exceptions into `is_error=True`.
- `app/tools/retrieve.py` — the closest model for a read tool: clamped `top_k`, an explicit not-found message, a savepoint around its database work.
- `app/db/builtin_tools.py` — `DEFAULT_ENABLED_TOOL_NAMES`, `first_row_per_name`, and the seeding/backfill SQL. **A new builtin must be seeded here and linked, or it is unreachable** — Phase 4 shipped inert once for exactly this reason.
- `app/chat/service.py` — `_BUILTIN_TOOL_CLASSES` is the tuple `_build_registry` iterates; a class missing from it can never be registered.
- `app/db/models/citation.py` — `MessageCitation` already has a nullable `product_id` column from Phase 1's schema, unused until now.
- Migrations at head `0009_seed_builtin_tools`. Yours start at `0010_products`.
- Web: `docs/DESIGN.md` is **binding** — read it first, update it after. `lib/format.ts`, `components/ui/`, `components/knowledge/DocumentsTable.tsx` (the closest existing table).

## File Structure

```text
apps/api/app/
├── products/
│   ├── schemas.py         # Task 1
│   ├── service.py         # Task 1
│   └── importer.py        # Task 3 — CSV/JSON → rows
├── db/models/product.py   # Task 1
├── rag/products.py        # Task 4 — three-way search
├── tools/products.py      # Task 5 — search_products, get_product
└── api/products.py        # Task 3 — multipart import
apps/web/src/app/dashboard/products/page.tsx   # Task 6
```

---

### Task 1: The `products` table and its service

**Files:** create `app/db/models/product.py`, `app/products/__init__.py`, `schemas.py`, `service.py`, `alembic/versions/0010_products.py`; modify `app/db/models/__init__.py`; test `tests/integration/test_product_service.py`, extend `tests/integration/test_migrations.py`.

**Interfaces produced:** `Product`, `ProductAvailability` enum, and `ProductService(session, tenant)` with `create`, `upsert_many`, `get`, `list_products`, `delete`.

**Requirements:**
- Columns per §3.4 exactly: `external_id`, `name`, `slug`, `description`, `category`, `price numeric(12,2)`, `currency char(3)`, `attributes jsonb`, `availability`, `stock_quantity`, `image_url`, `product_url`, `is_active`, `metadata jsonb`, `embedding vector(1536)`, `search_tsv`, timestamps.
- `down_revision = "0009_seed_builtin_tools"`; exactly one head; verify a `downgrade base` → `upgrade head` round-trip.
- `UNIQUE (organization_id, external_id)` — this is what makes re-import an upsert rather than a duplicate.
- Indexes: HNSW on `embedding` (`vector_cosine_ops`), GIN on `search_tsv`, **GIN on `attributes`** (the jsonb filter arm depends on it), and a btree supporting `(organization_id, category)` and price range scans.
- `search_tsv` is a **generated** column over `name`, `description` and `category` using the **`'english'`** configuration — Task 4 queries it with `websearch_to_tsquery('english', …)` and any other configuration returns zero keyword hits while the feature still appears to work.
- **`search_tsv` and the embedding deliberately exclude `price`, `stock_quantity` and `availability`** (PHASE-5.md §4). A price change must be an `UPDATE`, not a re-embed. Comment this where the generated column is defined, because it looks like an omission.
- `enable_rls` on the table.
- `upsert_many` is `ON CONFLICT (organization_id, external_id) DO UPDATE`, and must **not** clobber `embedding`/`search_tsv` bookkeeping in a way that silently leaves a stale vector — say in the report what it does and why.

**Tests:** cross-tenant isolation on `get` and `list_products`; the unique constraint makes a second import of the same `external_id` an update, not an insert; a price update does not change the embedding; RLS via the parametrized list in `test_migrations.py` (extend it, do not add a parallel block); the round-trip.

- [ ] Tests first, watch them fail, implement, migrate, gates, commit.

---

### Task 2: Product embedding

**Files:** create `app/products/embedding.py`; test `tests/integration/test_product_embedding.py`.

**Requirements:**
- `embeddable_text(product) -> str` composes name, description, category and **stable** attributes — never price, stock or availability.
- Batched embedding (default 64) with per-batch retry, mirroring `app/rag/ingest.py`'s `_embed_all`.
- Records `embedding_model` per product the way chunks do, so a mixed-provider catalogue is detectable.
- Re-embedding is idempotent: embedding a product whose stable fields are unchanged produces the same vector, and a caller can tell whether a re-embed is needed.

**Tests:** two products sharing vocabulary score higher than two unrelated ones (with `HashingEmbedder`, so this is meaningful rather than noise); changing only `price` leaves `embeddable_text` byte-identical; changing `description` changes it; batching splits correctly and a failed batch leaves no partially-embedded catalogue.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 3: Import

**Files:** create `app/products/importer.py`, `app/api/products.py`, `app/workers/` task; modify `app/workers/settings.py`; test `tests/integration/test_product_import.py`.

**Requirements:**
- `POST /api/v1/products/import` — multipart CSV or JSON. Authenticated, org-scoped. Returns **202** with an import record; the arq worker does the parsing, validation, upsert and embedding.
- Reuse the upload machinery Phase 3 built rather than re-deriving it: the capped ASGI `receive` that bounds the body mid-stream, `SUPPORTED_MIME_TYPES`-style validation, and storage under `settings.upload_dir`.
- **Per-row validation, batch-level success.** A malformed price on row 12 reports that row and continues. The response records how many rows succeeded, how many failed, and why — an import that rejects 4,000 products because of one bad row is not usable.
- Idempotent by `external_id` (Task 1's constraint), so re-importing the same file changes nothing.
- A row missing `external_id` is a hard error for that row; there is nothing to upsert against.

**Tests:** a valid CSV imports N products; a file with one bad row imports N-1 and reports the failure with its row number; re-importing the identical file leaves the count unchanged and updates nothing; cross-tenant — one org's import never touches another's rows; an unsupported mime type is 422; the worker's function list actually contains the new task.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 4: Three-way product search

**Files:** create `app/rag/products.py`; test `tests/integration/test_product_search.py`.

**Interfaces produced:** `ProductSearchService(session, tenant, embedder=None)` with `search(query=None, *, category=None, min_price=None, max_price=None, attributes=None, limit=10) -> list[ProductMatch]`.

**Requirements:**
- **Filters and ranking compose as AND** (PHASE-5.md §6): filters narrow the candidate set, then full-text and vector rank what survives. A £45,000 car is a wrong answer to "under £30,000" however similar it is.
- With a `query`, rank by fusing full-text and vector exactly as `app/rag/retrieve.py` does — RRF, `k=60`, 1-based ranks. Reuse that module's helpers rather than re-deriving; if the fusion is worth sharing, extract it rather than copying.
- With **no** `query`, filters alone with a deterministic order — a caller can legitimately ask for "everything under £30,000".
- `<=>` is cosine *distance*: smaller is better. Getting this backwards returns the least relevant products and still looks like it works.
- `attributes` filtering uses jsonb containment against the GIN index.
- `is_active = false` products never surface.
- Two-layer tenancy on every arm.

**Tests:** a price filter excludes a semantically perfect but out-of-range product — this is the test that proves AND composition, so make the excluded row the *best* match; category and attribute filters; query-less filtering; the `<=>` direction pinned by a test that fails if reversed; an inactive product never returns; cross-tenant isolation where the other org's product is the best match.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 5: `search_products` and `get_product`

**Files:** create `app/tools/products.py`; modify `app/chat/service.py` (`_BUILTIN_TOOL_CLASSES`), `app/db/builtin_tools.py`, `alembic/versions/0011_seed_product_tools.py`; test `tests/integration/test_product_tools.py`.

**Requirements:**
- Args per §7.2. `limit` clamped. `ToolContext.organization_id` scopes everything; the registry will reject an args model exposing it.
- **Results carry a `source`** naming the product row (§5.4's mechanism). Structured fields — price, currency, availability, attributes — not prose the model has to parse.
- **An empty result is an explicit message**, never an empty list.
- Both tools **seeded and linked**, following Task 7b's pattern: global builtin rows in a data migration, added to `DEFAULT_ENABLED_TOOL_NAMES` if they should be on by default, and a backfill for existing agents. **Decide and argue the default** — these are reads, unlike `create_lead`.
- Added to `_BUILTIN_TOOL_CLASSES`, or the registry can never construct them.
- Populate `message_citations.product_id` for products that reached the prompt, alongside the existing chunk citations. §5.4 rule 3, and what makes Phase 6 able to score this.

**Tests:** a product question reaches the provider with the tool offered, and the tool's structured result in the next request; the not-found message; `get_product` for another org's id returns the error result and leaks nothing; citations rows carry `product_id`; a fresh agent created through `AgentService.create_agent` resolves the new tools — assert on the captured `CompletionRequest`, not row counts.

- [ ] Tests first, watch them fail, implement, migrate, gates, commit.

---

### Task 6: The Products dashboard page

**Files:** create `apps/web/src/app/dashboard/products/page.tsx` and components; modify the GraphQL documents, `packages/shared/schema.graphql`, `docs/DESIGN.md`.

**Requirements:**
- **Read `docs/DESIGN.md` first and update it after.** Name no colour outside `globals.css`; `conventions.test.ts` enforces it. Reuse `lib/format.ts` and the existing table patterns.
- Import (drag-and-drop plus picker), showing accepted formats and the size limit **before** the user picks a file. Import status with per-row failures visible — the count alone is not actionable.
- A product list with price, availability and category; search and filter.
- **Product names, descriptions and attributes are customer-supplied. Render as text, never markup** — no `dangerouslySetInnerHTML`, no markdown pass. Test with a `<script>` payload.
- Regenerate `packages/shared/schema.graphql` and `generated.ts` — CI fails on a stale artefact.

- [ ] Implement, `tsc`/`eslint`/`vitest`/`build`, gates, commit.

---

## Final Verification

- [ ] `git fetch origin && git merge origin/main` — resolve conflicts.
- [ ] Full suites: **774 backend / 258 web** baseline still passing, plus the new ones.
- [ ] All gates clean; `alembic heads` single; `downgrade base` → `upgrade head` round-trips.
- [ ] `packages/shared/schema.graphql` regenerated and committed.
- [ ] `docker compose up -d --wait` and drive a real turn against a live model: import a small catalogue, ask "what do you have under £30,000", see `search_products` called and a priced answer. Then ask something the catalogue cannot answer and confirm the tool says so rather than the model inventing one.
- [ ] Whole-branch review, one fix wave, one scoped re-review, adjudicate residuals.
- [ ] Update `README.md`'s roadmap (§7 of PHASE-5.md renumbers it), `ARCHITECTURE.md` §9, and `PHASE-5.md` §8 with anything not delivered.
- [ ] Push and open a PR.
