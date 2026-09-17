# Phase 3 — Retrieval-Augmented Generation

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (§3.3 schema, §6 RAG architecture).
> That document is the binding spec; this one records what Phase 3 adds on top.

**Goal:** a business uploads its own documents, and the assistant answers from them — with
citations pointing at the passage that grounds each answer.

```text
Upload (multipart)  ──▶  documents row, status=pending  ──▶  enqueue
                                                              │
                                     ┌────────────────────────┘
                                     ▼
              extract ──▶ normalize ──▶ chunk ──▶ embed ──▶ store
                                                              │
Playground question ──▶ retrieve (vector + full-text, fused) ─┘
                              │
                              ▼
                 context + citations ──▶ LLM ──▶ answer
```

---

## 1. Scope

**In:** the embedding provider abstraction, the ingestion pipeline, `documents` and
`document_chunks` with pgvector, hybrid retrieval, retrieval wired into chat with
citations, an upload endpoint, and a Knowledge page.

**Out:** products as structured knowledge (§3.4), tools, the multi-step agent loop, and
evaluation. Those are Phases 4 and 5.

**Deliberately deferred to Phase 4 — say it plainly so nobody thinks it was forgotten.**
`docs/ARCHITECTURE.md` §5.2 argues retrieval should be a *tool* the model chooses to call.
Phase 3 does **not** do that: it retrieves before every chat turn and injects the result.
That is the simpler half, it makes the feature demonstrable, and the retrieval service it
produces is exactly what Phase 4's `retrieve_knowledge` tool will wrap. Until then the
agent cannot decide *not* to retrieve, so it pays the retrieval cost on "hi" as well as on
a real question.

---

## 2. The embedding provider, and the problem it has to solve here

### 2.1 Why it is a separate interface

Per §6.4: **Anthropic has no embeddings API.** An organization can reasonably run Claude
for chat and something else for embeddings, so `EmbeddingProvider` is its own protocol, not
a method on `LLMProvider`.

```python
class EmbeddingProvider(Protocol):
    name: str
    dimensions: int
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Batch-shaped because ingesting a document embeds hundreds of chunks, and one request per
chunk is the difference between a fast ingest and a rate-limited one.

### 2.2 The constraint this phase actually faces

**No embeddings API is reachable in this environment.** `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` are unset; Anthropic has none to offer; and the configured
`OPENROUTER_API_KEY` fronts chat completions — whether it exposes an embeddings endpoint is
unverified and should be checked before anyone relies on it.

Phase 2 answered the equivalent problem with `FakeProvider`, which replays a canned script.
**That trick does not work here**, and the reason is the most important design point in
this phase.

A chat fake only has to produce *some* text. An embedding fake has to produce vectors whose
**cosine similarity means something**, because every retrieval test asks "did the right
chunk come back?" A fake that hashes each input to a random vector answers that question
with noise: the top-scoring chunk is arbitrary, so a passing retrieval test proves nothing,
and the playground would cite whichever chunk happened to land nearby.

### 2.3 `HashingEmbedder` — a real, if modest, embedder

So the default is not a fake. It is a **hashed bag-of-words** embedder: tokenize, hash each
token into one of `dimensions` buckets, weight by frequency, L2-normalize.

That gives genuine lexical similarity — two passages sharing vocabulary score high, two
unrelated ones score near zero — with no network, no model download, and no dependency.
Retrieval tests become meaningful, and a clone with no API key can upload a document and
get a grounded answer.

**Its limits, stated honestly.** It is lexical, not semantic. "car" and "automobile" are
unrelated to it. It sets a floor for retrieval quality, and it is not a demonstration of
what real RAG does. Swapping in `OpenAIEmbeddingProvider` is a config change, and §2.4
explains why that swap costs nothing at the schema level.

`OpenAIEmbeddingProvider` (`text-embedding-3-small`) is implemented alongside it and
unit-tested against a mocked client, the same way the chat adapters are — so adding a key
turns it on rather than starting the work.

### 2.4 Dimensions stay 1536

`HashingEmbedder` emits 1536-dimensional vectors even though nothing forces it to, because
`text-embedding-3-small` does. Matching the real model's width means switching providers is
a re-embed, not a migration — and `pgvector` columns have a fixed dimension, so a mismatch
would mean `ALTER TABLE` on live data.

Chunks record `embedding_model`, so a mixed-provider corpus is detectable rather than
silently incoherent. Mixing is still wrong — cosine similarity across two embedding spaces
is meaningless — and re-ingestion is the fix.

---

## 3. Ingestion

Upload returns immediately with `status=pending` and enqueues. A 200-page PDF must not hold
an HTTP connection open, and the work must survive a deploy — which is why this is an
**arq worker** rather than a FastAPI `BackgroundTask`. Background tasks die with the process
and leave a row stuck in `processing` with nobody retrying it. The `documents.status`
column only means something if a real queue backs it.

| Stage | Notes |
|---|---|
| extract | `.txt`, `.md`, `.html`, `.pdf`, `.docx`. Page offsets preserved for PDFs so citations can name a page. |
| normalize | collapse whitespace, de-hyphenate line breaks, strip boilerplate |
| chunk | structure-aware: split on headings, then ~500 tokens with 15% overlap, never mid-sentence |
| embed | batched, retried with backoff, per-batch failure isolation |
| store | content + vector + generated `tsvector` + metadata (page, section, char offsets) |

**Idempotence.** `documents.checksum` is the SHA-256 of the file. Re-uploading an unchanged
file is a no-op rather than a second copy plus a second embedding bill.

**Failure is recorded, not swallowed.** A failed stage sets `status=failed` and writes the
error to the row, so the dashboard can show *why* rather than leaving a document pending
forever.

---

## 4. Retrieval

```text
query → rewrite (resolve pronouns against recent turns)
   ├── vector:  embed → pgvector cosine, top 20
   └── keyword: websearch_to_tsquery → ts_rank_cd, top 20
   ↓
fuse: Reciprocal Rank Fusion, score = Σ 1 / (60 + rank_i)
   ↓
filter below min_score → take top_k (default 5)
   ↓
assemble context with chunk ids for citation
```

**Why hybrid rather than vector alone.** Pure vector search fails on exact identifiers —
part numbers, trim levels, "Camry LE vs Camry SE" — because those distinctions are a few
characters that embeddings smooth over. Full-text catches exactly those. RRF merges two
rankings without needing tuned weights.

It matters more here than it would with a strong embedder: `HashingEmbedder` is itself
lexical, so vector and keyword results correlate. The fusion still earns its place — it is
what stops retrieval quality collapsing when the embedder is swapped for a semantic one and
the two rankings start to disagree.

**Tenancy.** Retrieval runs on the tenant-bound session, so RLS scopes it. But per the
finding recorded in Phase 2, an `INSERT` establishing a foreign key is *not* protected by
RLS — so chunk writes during ingestion do an explicit ownership check on the parent
document first.

---

## 5. Grounding and citations

Retrieved chunks enter the prompt in a delimited block, labelled as reference material and
explicitly **not** instructions — the partial mitigation for prompt injection that §10 of
the architecture doc already flags as unsolved.

Every chunk that reached the prompt is recorded in `message_citations` with its rank and
score. That is what makes Phase 5's faithfulness scoring possible: without a record of what
the model was shown, "did it answer from the sources?" is unanswerable after the fact.

The SSE stream gains a `citations` event, and the playground renders sources under the
answer.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| The default embedder is lexical, so retrieval looks worse than real RAG | Stated plainly above and in the Knowledge UI; `OpenAIEmbeddingProvider` ships alongside and is a config change |
| Mixed embedding models in one corpus are silently incoherent | `embedding_model` recorded per chunk; a mismatch is detectable and re-ingestion is the fix |
| pgvector's fixed dimension | Pinned at 1536 to match the real model; a change means re-embedding, which is expected |
| Ingestion failures leave documents stuck | `status` + `error` on the row, surfaced in the dashboard, with an explicit retry |
| Prompt injection from uploaded documents | Delimited, labelled untrusted; prices and policies still come from tools, not prose. Partial — no technique is complete |
| Retrieval on every turn costs tokens even for "hi" | Accepted for Phase 3; Phase 4 makes retrieval a tool the model chooses |
