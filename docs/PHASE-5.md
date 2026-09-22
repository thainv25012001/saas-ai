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

The embedding covers **name, description and stable attributes**. It excludes **price,
stock quantity and availability**.

That is a deliberate asymmetry and the reason for it is operational: prices change. If the
embedded text included the price, every price change would require re-embedding the row —
an API call and a write — to keep the vector honest. Excluding volatile fields makes a
price change a plain `UPDATE` and nothing more, while the semantic question those fields
never answered ("something safe for my family") is unaffected.

The cost: "cheap hybrids" will not match semantically on cheapness. That is the filter
arm's job, and §3 is the argument for having one.

`search_tsv` follows the same split for the same reason.

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
| An embedding-less upsert (§4's price/stock sync, or a two-phase import racing its own embed job) changes `name`/`description`/`attributes` while the stored `embedding` doesn't move | `Product.embedding_source_hash` records what text the stored vector was actually computed from; `ProductService.upsert_many` recomputes it on every embedding-less write and sets `embedding_stale` on a mismatch, logging `product.embedding_stale`. Detected and queryable (`WHERE embedding_stale`), not corrected automatically — nothing re-embeds the row yet, so semantic search can still rank it on its old content until something (a caller, or a future reconciliation job) does. |
| `attributes jsonb` becomes a dumping ground with no schema | GIN-indexed and filterable, but unvalidated by design. A per-vertical schema is a product decision this phase does not make. |
| Embedding excludes price, so "cheap X" misses semantically | §4 — deliberate; the filter arm is the answer, and §3 is why it exists |
| A large import blocks the worker used by document ingestion | Same arq worker, same queue. Acceptable at this scale and named here so it is a known limit rather than a surprise. |
| Products and documents disagree about the same fact | Nothing reconciles them. The tool is authoritative by convention only. |
