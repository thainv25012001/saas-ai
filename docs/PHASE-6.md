# Phase 6 — Evaluation

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §3.7 (evaluation schema) and §10 risk
> 10. That document is the binding spec; this one records what Phase 6 adds and where it
> deliberately departs from §3.7.

**Goal:** a business owner can tell whether a change made the assistant better or worse —
a new prompt version, a different model, a re-imported catalogue — by running the same
questions against it and reading a score, instead of chatting in the playground and
trusting an impression.

```text
dataset ──▶ cases (question + expectations)
                     │
start run (agent, pinned prompt version, pinned model, optional judge)
                     │  REST: commit, then enqueue
                     ▼
arq worker ──▶ for each case:
                 ChatService.send(...)   ← the production turn, unmodified
                 inside a transaction that is ALWAYS rolled back
                     │ answer · tool calls · citations · usage
                     ▼
                 scorers (deterministic) + judge (LLM, optional)
                     ▼
                 eval_results row (committed on its own) + usage_events row
                     ▼
run summary ──▶ dashboard: pass rate, per-scorer means, cost; compare two runs
```

---

## 1. Scope

**In:** datasets and cases, evaluation runs executed by the worker against the real chat
path, four deterministic scorers, an optional LLM-as-judge, pinning of prompt version and
model per run, cancellation, a GraphQL surface, and an Evaluations section in the
dashboard with a run-vs-run comparison.

**Out:** multi-turn cases (every case is a single customer message in a fresh
conversation), scheduled or CI-triggered runs, scoring of real production conversations,
dataset import/export, and statistical significance across repeated runs. §9 records each.

---

## 2. The one decision everything else follows from

**An evaluation runs the production turn — `ChatService.send` — not a re-implementation
of it.** A harness that calls the provider directly would measure a different system:
no tool grants, no registry, no prompt rendering, no retrieval thresholds. The thing a
prompt change can break is exactly the thing a shortcut would skip.

That creates one problem: the production turn *writes*. It creates a conversation, a
message pair, tool-call and citation rows, a `usage_events` row, and — if `create_lead` is
granted and the model calls it — a real lead in the organization's pipeline.

**Resolution: each case's turn runs in a tenant transaction that is always rolled back.**
Inside it, the agent sees exactly what production would show it, including a
`create_lead` that genuinely succeeds. After the turn, the runner reads what it needs (the
full tool-call payloads, from `message_tool_calls`, before the rollback) and then discards
everything. A second, independent transaction writes the `eval_results` row and one
`usage_events` row for the spend.

Rejected alternatives:

| Alternative | Why not |
|---|---|
| Keep the conversation, add an `evaluation` channel | Leads still get written; every conversation list, lead list and future analytics query needs a filter someone will forget. |
| A dry-run flag on write tools | The model then sees a different tool result from production, which is the thing under test. Every future write tool must remember to honour the flag. |
| Call the provider directly | Measures a different system (above). |

The cost: an eval result does not link to a stored transcript. It stores what a transcript
would be read for — the answer, every tool call with its arguments and outcome, and every
citation — on the result row itself.

Evaluation spend is real spend, so it still reaches `usage_events` — with
`conversation_id` NULL, since the conversation never persisted.

---

## 3. Schema

Departures from ARCHITECTURE.md §3.7 are marked ★.

```text
eval_datasets
  id, organization_id, name, description, created_at, updated_at
  UNIQUE (organization_id, name)

eval_cases
  id, organization_id, dataset_id → eval_datasets ON DELETE CASCADE,
  question text,
  reference_answer text NULL,           ★ renamed from expected_answer: it is judged, not matched
  required_phrases text[] DEFAULT '{}', ★ deterministic answer check (§4)
  expected_tool_names text[] DEFAULT '{}',
  expected_document_ids uuid[] DEFAULT '{}',
  expected_product_ids uuid[] DEFAULT '{}', ★ products did not exist when §3.7 was written
  tags text[] DEFAULT '{}', created_at, updated_at

eval_runs
  id, organization_id, dataset_id → eval_datasets ON DELETE CASCADE,
  agent_id → agents ON DELETE CASCADE,
  prompt_version_id → prompt_versions ON DELETE SET NULL,   -- pinned at start
  provider, model,                                          -- pinned at start
  judge_provider NULL, judge_model NULL,                    ★ the judge is per run
  status enum(pending, running, completed, failed, cancelled), ★ cancelled added
  case_count int, completed_count int,                      ★ progress
  summary jsonb, error text NULL,
  triggered_by → users ON DELETE SET NULL,
  started_at NULL, finished_at NULL, created_at, updated_at

eval_results
  id, organization_id, run_id → eval_runs ON DELETE CASCADE,
  case_id → eval_cases ON DELETE SET NULL,
  question text,                        ★ denormalised: a result outlives its case
  answer text, error text NULL,
  scores jsonb,                         -- {"tool_selection": {...}, ...} (§4)
  passed bool,
  tool_calls jsonb,                     -- [{name, arguments, is_error, excerpt}]
  cited_document_ids uuid[], cited_product_ids uuid[],  ★ split from retrieved_chunk_ids
  prompt_version_id NULL, latency_ms, input_tokens, output_tokens, cost_usd NULL,
  created_at
  UNIQUE (run_id, case_id)              -- what makes a retried job resumable
```

All four are tenant-owned: `organization_id NOT NULL`, `enable_rls`, and the explicit
`organization_id` predicate on every query (§2.3's two layers).

`expected_document_ids` and `expected_product_ids` are arrays, so no foreign key protects
them. The service validates on every write that each id belongs to the organization — the
same FK-bypass reasoning as elsewhere, applied to a column that has no FK at all. A
document deleted later simply stops being citable, and the case starts failing, which is
the correct signal.

A case must carry **at least one expectation** (`reference_answer`, `required_phrases`,
`expected_tool_names`, `expected_document_ids` or `expected_product_ids`). A case with
none can only ever "pass", which is worse than not having it.

---

## 4. Scoring

Each scorer returns `{score: 0..1, passed: bool, detail}` or is **not applicable** when its
expectation is empty. A case passes when **every applicable scorer passes and the turn did
not error.** Not-applicable scorers are left out of the case, and out of the run's means.

| Scorer | Applies when | Score | Passes when |
|---|---|---|---|
| `required_phrases` | phrases given | fraction found in the answer | all found |
| `tool_selection` | tool names given | fraction of expected tools called *successfully* | all called |
| `document_recall` | document ids given | fraction cited | all cited |
| `product_recall` | product ids given | fraction cited | all cited |
| `judge` | the run has a judge **and** the case has a reference answer | correct 1 · partial 0.5 · incorrect 0 | `correct`, and grounded |

**`required_phrases` normalisation:** casefold, Unicode NFKC, punctuation to spaces,
whitespace collapsed, and a thousands separator between digits removed — so `$35,000`
matches `35000` and `3-year` matches `3 year`. Matched on word boundaries so `3 years`
does not match inside `13 years`. That is the whole of it: no stemming and no synonyms. ARCHITECTURE.md
§10 risk 10 still stands; the phrase list is how an author says which surface forms count,
and the judge is how they avoid having to.

**`tool_selection` counts only successful calls.** A `search_products` that errored did
not answer anything. Calls to tools that were not expected are reported in `detail` and
do not fail the case: the model looking something up an extra time is not wrong.

**The judge** (`app/evaluations/judge.py`) makes one `generate_structured` call per case
with the question, the reference answer, the agent's answer and the evidence the agent
actually saw — the full content of its tool results, bounded to 12,000 characters. It
returns:

```python
class JudgeVerdict(BaseModel):
    correctness: Literal["correct", "partially_correct", "incorrect"]
    grounded: bool      # every factual claim is supported by the evidence, or it declines
    rationale: str      # at most 1,000 characters are stored
```

`grounded` is what makes the judge measure hallucination as well as correctness: an
answer can match the reference and still invent a price the evidence never contained.
An unanswerable case is written as a reference answer such as *"The assistant says it does
not have this information."* — the judge then rewards an honest refusal and penalises an
invented answer, which is the behaviour ARCHITECTURE.md §5.2 asks for.

Everything the judge reads (the answer and the evidence) is written by the model or by
the customer, so it goes inside a per-call `secrets.token_hex(8)` fence, and the judge's
system prompt says fenced text is material to grade, not instructions — the same defence
Phase 3 uses for retrieved passages, with the same honest limit: it is a mitigation, not
a guarantee. A judge failure (provider error, invalid structure) marks that scorer
`error` and fails the case; it never turns into a pass.

The judge's own usage is priced and recorded as a separate `usage_events` row.

---

## 5. Runs

**Starting** is `POST /api/v1/evaluations/runs` (REST, not a GraphQL mutation — see §7).
It validates that the dataset has between 1 and 200 cases, that the agent belongs to the
organization, that the prompt version (if named) belongs to that agent's prompt, and that
no other run of the same dataset is `pending` or `running`. It then **pins**:

- `prompt_version_id` — the version named, else the agent's active version *at start*.
  A version activated halfway through a run cannot split it in two.
- `provider`/`model` — the pair named, else the agent's own pair at start.

Pinning the prompt version requires one change to production code: `ChatService.send`
gains an optional `prompt_version_id`, validated to belong to the agent's prompt, used in
place of the active version. That also makes the flow this phase exists for possible:
**draft a version, evaluate it, and activate it only if it scores better** — without
activating it first.

**Execution** (`run_evaluation_task`, arq): cases run one after another, not in parallel —
a provider's rate limit is shared with the organization's live traffic, and a run is not
latency-sensitive. Before each case the runner re-reads the run's status, so cancelling is
checked between cases rather than mid-turn. A case already holding a result for this run is
skipped, so an arq retry after a crash resumes rather than re-billing. The job gets its
own timeout (1 hour) rather than the worker's 10-minute default.

A case whose turn errors (provider timeout, rate limit, step limit) records the error and
fails; the run continues. Only a failure outside any one case (the run row vanished, the
database is unreachable) fails the run.

**Summary**, written when the run completes:

```jsonc
{
  "passed": 17, "failed": 3, "errored": 1, "pass_rate": 0.85,
  "scorers": {"tool_selection": {"mean": 0.95, "passed": 19, "applicable": 20}, ...},
  "cost_usd": "0.0412",          // null if any priced component was unpriced
  "mean_latency_ms": 2140
}
```

---

## 6. Comparing runs

The run page compares against any other completed run of the same dataset, case by case:
**regressed** (passed → failed), **improved**, **unchanged**, or **new** (a case added
since). This is computed in the browser from two runs' results; neither side is large enough
to justify a server endpoint for it.

---

## 7. Why starting a run is REST

A GraphQL operation's transaction commits only after the resolver returns
(`app/graphql/context.py::build_context`). A resolver that enqueued the job would therefore
race its own commit: the worker opens an independent connection and can look for the run
row before it exists. Product import solved the same problem by committing first and
enqueuing after, in a REST handler (`app/api/products.py`). Starting a run does the same.
Everything else — dataset and case CRUD, listing runs and results, cancelling — is GraphQL.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| A run costs real money, and a 200-case run with a judge is 400+ LLM calls | 200-case cap, one active run per dataset, cost recorded per case and in `usage_events`, and shown on the run before and after. |
| The judge is itself a model and can be wrong, or be argued with by the text it grades | Fenced input; deterministic scorers alongside it; its rationale is shown so a human can overrule it; the judge's model is chosen per run and recorded. |
| The rolled-back transaction hides a write a case depended on | By design nothing a case does survives it. Every case starts from the same catalogue and knowledge, which is what makes two runs comparable. |
| Non-determinism: the same run twice gives different scores | Recorded, not solved — see §9. Temperature comes from the agent, so an agent at 0 is as repeatable as its provider allows. |
| An eval turn and a live customer share rate limits | Cases run one at a time. |

---

## 9. Not delivered

| Not delivered | Why, and where it goes |
|---|---|
| Multi-turn cases | Every case is one message in a fresh conversation. Follow-up behaviour ("and in red?") is not measured. Needs a case to be a script of turns. |
| Repeated runs, variance, significance | One run is one sample. A 3-point difference between two runs may be noise. |
| Scoring real production conversations | Only authored cases are scored. Scoring live traffic needs sampling and privacy decisions that come with the widget (Phase 8). |
| Dataset import/export | Cases are authored one at a time in the dashboard. |
| Stored transcripts for eval turns | §2 — the result row holds what a transcript is read for. |
| Stuck-run recovery | A run whose worker is killed stays `running` until the arq retry resumes it. If every retry is exhausted, it stays `running` and blocks new runs of its dataset; cancelling it is the way out. |
