# Phase 5 — Products as structured knowledge

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (§3.4 products, §5.4 grounding, §7.2
> tools). That document is the binding spec; this one records what Phase 5 adds.

**Goal:** the assistant can answer *what do you sell, what does it cost, and is it in
stock* — from data, not from prose that happens to mention a price.

```text
import ──▶ products row ──▶ embed(name + description + attributes) ──▶ pgvector
                                                                  └─▶ search_tsv
                                    │
question ──▶ search_products ──▶ filters ∧ (full-text ∨ vector) ──▶ ranked rows
                                    │
                             structured result ──▶ model ──▶ answer
```

---

## 1. Scope

**In:** the `products` table, an import path, embedding, three-way search (exact filters,
full-text, semantic), the `search_products` and `get_product` tools, and a Products
dashboard page.

**Out:** evaluation (now Phase 6), MCP (Phase 7), and the SaaS layer (Phase 8) — see §7 for
why the roadmap renumbered.

---

## 2. The rule this phase exists to make true, and why it still will not be

`docs/ARCHITECTURE.md` §5.4 states it plainly:

> Prices, availability, and specs are only ever reported from **tool results**, never from
> document prose.

**That rule is currently unenforceable, and Phase 5 does not fully enforce it either.**
Saying so up front matters more than the feature.

Today there are no product tools, so the only way the agent can answer "how much is the
Camry LE" is from a retrieved document chunk. Phase 4 shipped a system prompt that already
instructs it to *recommend products*. So the rule is violated by construction.

Phase 5 adds the tools — and the violation does not go away, it changes shape. A customer
uploads a PDF price list. `retrieve_knowledge` retrieves it, correctly, because it is a
document and the question was about price. The model now has two sources for the same
fact: a chunk saying `$28,000` and a tool that would say `28499.00`. Nothing in the
architecture stops it reading the chunk.

**What Phase 5 actually does about it,** in descending order of how much it is worth:

1. **Make the tool the easier path.** `search_products` returns structured fields the model
   does not have to parse out of prose, and every result carries a `source` naming the
   product row it came from. A tool answer is cheaper to use correctly than a chunk.
2. **Say so in the prompt.** Necessary, insufficient, and this project has repeatedly found
   prompt rules alone do not hold.
3. **Record which it used.** `message_citations` already distinguishes a document chunk
   from a product (the column exists, nullable, from Phase 1's schema). Populating it for
   product results is what makes the question *answerable after the fact* rather than
   assumed — which is the whole reason Phase 6 is evaluation.

**What it does not do:** suppress document chunks that contain prices. That would need
retrieval to know a chunk is price-like, which is a classifier, and a wrong one silently
drops legitimate context. Recorded as not delivered (§8) rather than pretended away.

---

## 3. Why products are not just documents

A reasonable objection: products are text, retrieval already works, why a second subsystem?

Because the three queries a sales agent actually receives need three different mechanisms,
and only one of them is similarity:

| Question | Needs |
|---|---|
| "under £30,000, seats seven" | **exact filters** — a numeric predicate, not a vector |
| "Camry LE vs Camry SE" | **full-text** — the distinction is a few characters an embedding smooths over |
| "something safe for my family" | **semantic** — no shared vocabulary with the listing |

Documents give you the third and a weak version of the second. A price filter over prose is
not a thing that works. `attributes jsonb` with a GIN index carries the heterogeneity —
`{"seats": 7, "fuel": "hybrid"}` for a dealership, something else entirely for a
retailer — without a table per vertical.

---

## 4. What gets embedded, and what deliberately does not

The embedding covers **name, description and attributes**. It excludes **price,
stock quantity and availability** — and category, which full text covers instead.

"Attributes" means the whole `attributes` object: the code does not pick out a stable
subset. The volatile fields have their own columns, so `attributes` holds stable facts
*by convention* — but a customer who puts a changing value there (a `sale_price`, say)
makes every change to it a re-embed.

That is a deliberate asymmetry and the reason for it is operational: prices change. If the
embedded text included the price, every price change would require re-embedding the row —
an API call and a write — to keep the vector honest. Excluding volatile fields makes a
price change a plain `UPDATE` and nothing more, while the semantic question those fields
never answered ("something safe for my family") is unaffected.

The cost: "cheap hybrids" will not match semantically on cheapness. That is the filter
arm's job, and §3 is the argument for having one.

`search_tsv` follows the same split for the same reason. It covers name, description and
category — not attributes, which are filtered exactly (`@>`) rather than searched as text.

---

## 5. Import

Products arrive as a **CSV or JSON upload**, reusing Phase 3's ingestion shape: upload
returns immediately, an arq job does the work, and the row carries its own status and
error. A catalogue is thousands of rows and an HTTP request is the wrong place to embed
them.

`UNIQUE (organization_id, external_id)` makes re-import an **upsert**, not a duplicate —
which is what makes a nightly sync from a customer's own system possible later without
this phase building one.

Rows that fail validation are reported per row and do not abort the batch. An import that
rejects 4,000 products because row 12 has a malformed price is not usable.

The job runs in two phases. First it upserts the rows, in chunks, each committed on its own;
then it embeds the rows that need a vector, separately. The split is what keeps an
embedding-provider outage from costing the customer their prices and stock: if embedding
fails, the rows stay imported but unembedded — found by filters and full text, badged
"Not yet searchable by meaning" in the dashboard — the import completes with a warning
saying so, and re-importing the file retries the embedding. A chunk that fails at the
database is retried row by row so only the offending rows are reported; nothing else is
retried per row.

---

## 6. The tools

Per §7.2:

| Tool | Arguments | Returns |
|---|---|---|
| `search_products` | `query?`, `category?`, `min_price?`, `max_price?`, `attributes?`, `limit?` | matching products with price, availability, attributes |
| `get_product` | `product_id` | the full record for one product |

Both are reads, so both inherit Phase 4's machinery unchanged: the per-turn registry, the
`agent_tools` grant that decides whether the agent may call them at all, the shared-session
lock, the savepoint, and the `is_error` contract.

**`search_products` returning nothing is an explicit result, not an empty list** — the same
reason `retrieve_knowledge` says so. "No product matched under £30,000" is an answer; an
empty payload invites the model to invent one.

But a filter is also the model's *guess* at the catalogue's vocabulary: it cannot see the
organization's category names before it searches. So the empty result echoes the filters
that were applied and lists the catalogue's categories (at most 25), and tells the model to
retry with a corrected value before telling the customer nothing matches. The category
filter ignores case; it is otherwise still an exact name.

Filters and search compose as **AND**: a filter narrows, then ranking orders what survives.
A customer who says "under £30,000" has stated a constraint, not a preference, and a
semantically similar £45,000 car is a wrong answer however well it matches.

---

## 7. The roadmap renumbered

`README.md` listed Phase 5 as Evaluation while its own Phase 4 row said products move to
Phase 5. Both cannot be true. Products go first because the product is a *sales* agent and
it currently cannot state a price it is allowed to state; evaluation measures quality
without adding capability, and it has more to measure once products exist. Evaluation
becomes Phase 6, MCP 7, SaaS features 8.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| The model quotes a price from a document chunk instead of a tool | §2 — partial by construction. Structured results, a prompt rule, and a record of which source was used. Not solved. |
| A stale catalogue answers confidently | `products.updated_at` is shown in the dashboard; import is an upsert so a re-sync corrects it. No staleness warning in the tool result — recorded as not delivered. |
| An embedding-less upsert (§4's price/stock sync, or an import whose embedding phase failed) changes `name`/`description`/`attributes` while the stored `embedding` doesn't move | `Product.embedding_source_hash` records what text the stored vector was actually computed from; `ProductService.upsert_many` recomputes it on every embedding-less write and sets `embedding_stale` on a mismatch, logging `product.embedding_stale`. Detected and queryable (`WHERE embedding_stale`), and a re-import re-embeds the row; nothing else does, so semantic search can still rank it on its old content until something (a re-import, or a future reconciliation job) does. |
| `attributes jsonb` becomes a dumping ground with no schema | GIN-indexed and filterable, but unvalidated by design. A per-vertical schema is a product decision this phase does not make. |
| Embedding excludes price, so "cheap X" misses semantically | §4 — deliberate; the filter arm is the answer, and §3 is why it exists |
| A large import blocks the worker used by document ingestion | Same arq worker, same queue. Acceptable at this scale and named here so it is a known limit rather than a surprise. |
| Products and documents disagree about the same fact | Nothing reconciles them. The tool is authoritative by convention only. |

---

## 9. Not delivered

Recorded here rather than left to be discovered, per the plan's Final Verification. None
of these is a defect; each is a boundary drawn deliberately or a cost accepted at this
project's scale.

| Not delivered | Why, and where it goes |
|---|---|
| **The model can still quote a price, or any other product fact, from a retrieved document chunk instead of a tool.** No chunk is suppressed for containing a price-shaped string, and products and documents are never reconciled against each other — a PDF price list and the `products` table can disagree and nothing notices. §2's rule is not enforced by this phase; it is made cheaper to follow and easier to audit after the fact, not closed. | §2 argues why suppression is the wrong fix (it needs a classifier, and a wrong one silently drops legitimate context) and names what actually shipped instead: a structured tool result that is cheaper to use correctly than parsed prose, a prompt rule (§2 itself says prompt rules alone do not hold), and a citation recording which source answered. None of those stop the model from reading the chunk. No phase currently reconciles the two sources. |
| **No automatic re-embedding of `embedding_stale` rows.** `ProductService.upsert_many` detects and records the mismatch (`embedding_stale=true`, logged as `product.embedding_stale`) but nothing reads that flag on its own to re-embed the row — it stays stale until a re-import of it (or another write carrying a vector) recomputes it. The same holds for rows an import left unembedded because the embedding provider failed. The dashboard badges affected rows ("Search index out of date"); nothing else acts on it. | Detection was this phase's scope (§8's third risk); a reconciliation job that walks `WHERE embedding_stale` and re-embeds is future work with no phase assigned. |
| **No staleness warning in tool results.** Neither `search_products` nor `get_product`'s result payload carries `embedding_stale` or `embedding_source_hash` — a stale row is returned, and ranked on its old vector, exactly like a fresh one. Only the dashboard shows the badge; the model answering a live question never sees it. | The dashboard's indicator (Task 6) and the tool contract (Task 5) were built independently and never reconciled with each other. |
| **Dashboard product search is `ILIKE` on `name`/`external_id`, with no trigram index.** A sequential scan within one organization's rows on every keystroke (debounced). Fine at the catalogue sizes this project has been run against; degrades as a catalogue grows. | A `pg_trgm` GIN index is the fix, not yet added. |
| **`ProductImport.errors` is bounded on the wire, not in the database.** The GraphQL field returns at most 200 rows, sorted, but each poll still loads the *full*, unbounded `errors` jsonb column for every import row on the page before truncating it in Python. `failed_count` already reports the true total, so the wire cap is sufficient for what the dashboard shows, but the read cost scales with the worst import ever run, not with what is displayed. | Storage-level bounding (or a separate errors table) was not attempted. |
| **A large product import shares the arq queue and worker with document ingestion.** `import_products_task` is registered on the same `WorkerSettings.functions` list and the same Redis queue as `ingest_document_task` (`app/workers/settings.py`). A very large catalogue import can delay document ingestion jobs behind it, and vice versa. | Acceptable at this project's scale; a second queue or worker pool per job type is the fix if either workload grows enough to interfere with the other. |
| **No per-vertical `attributes` schema.** `attributes jsonb` is GIN-indexed and filterable but unvalidated — nothing stops a row from having inconsistent or missing keys for its own category. | §3 and §8 name this as a deliberate deferral: a schema per vertical (car seats vs. clothing sizes) is a product decision this phase does not make. |
| **The vector arm ignores `embedding_model`.** Neither `ProductSearchService`'s vector arm nor `needs_reembedding` looks at which provider produced a stored vector. After switching embedding provider, every product vector stays in the old model's space and is compared, silently, against a query embedded in the new one — ranking becomes noise while nothing reports a problem. Documents have the same pre-existing gap. | Task 2 added `embedding_model` precisely to name this hazard; using it (filter the vector arm to the current model, and treat a model mismatch as needing re-embedding) is future work with no phase assigned. |
| **Uploaded import files are never deleted.** `store_import_bytes` writes each catalogue under `upload_dir/product_imports/`, and nothing removes it after the job finishes. Unlike documents, there is no retry endpoint to justify keeping the bytes, so customer catalogue files accumulate on disk indefinitely. | Deleting the file once the import reaches a terminal state (or a retention sweep) is the fix, not yet written. |
| **A stuck import makes the Products page poll forever, with no retry.** An import whose enqueue failed stays `pending`, and one whose worker was hard-killed stays `processing`; neither ever reaches a terminal state, so `shouldPollImports` keeps polling while the tab is visible. There is no retry button (documents have one). | A stuck-job sweeper or a retry action, plus a polling cap, is future work. |
| **An import that fails partway shows "Failed" with no counts.** `mark_failed` records only the error. Phase 1 commits chunk by chunk, so an import cancelled or killed by a non-row error after some chunks committed has landed those products, but the record shows no counts — and a cancellation (the job timeout) during the embedding phase marks an import "Failed" even though every row is already in. Only an embedding-provider *error* is reported as the completed-with-warning outcome. | Recording the running counts on failure, and treating a cancelled embedding phase like a failed one, is the fix; not attempted. |
