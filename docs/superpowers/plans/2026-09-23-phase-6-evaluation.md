# Phase 6 — Evaluation: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a business owner can tell whether a prompt, model or catalogue change made the assistant better or worse, by running a fixed set of questions against the real chat path and reading a score.

**Architecture:** `eval_*` tables (RLS) and an `EvaluationService`; an arq job that drives the unmodified `ChatService.send` once per case inside a transaction that is always rolled back, then scores the observation with deterministic scorers plus an optional LLM judge, and commits one `eval_results` row per case; REST to start a run (commit-then-enqueue), GraphQL for everything else; a `/dashboard/evaluations` section with run-vs-run comparison.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Strawberry, arq, Pydantic v2; Next.js App Router, urql + graphql-codegen, Tailwind v4, vitest.

**Spec:** [docs/PHASE-6.md](../../PHASE-6.md), extending [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) §3.7. Read both before starting any task.

## Global Constraints

- Python **3.12**, `uv`; API code under `apps/api/app/`. **GNU `make` is NOT available** — run underlying commands directly.
- Async tests use **`anyio` only** (`pytestmark = pytest.mark.anyio`); `pytest-asyncio` is deliberately absent.
- The suite runs with `filterwarnings = ["error"]` — any `DeprecationWarning` fails it.
- **No test may make a network call.** Use `FakeProvider` (`turns=[...]` for tool calls), `HashingEmbedder`, or a stub provider class.
- **Integration tests need all three overrides in every shell invocation** (each Bash call is a fresh shell):
  ```sh
  export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai"
  export MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai"
  export REDIS_URL="redis://localhost:6379/0"
  ```
  Start infra with `docker compose up -d --wait db redis`. **Check no other `pytest` is running before starting one** — concurrent runs against the shared database produce phantom failures.
- Tenant-owned tables get `organization_id UUID NOT NULL` + RLS via `enable_rls(op, table)`. Never inline policy SQL.
- **Two-layer tenancy is mandatory:** an explicit `organization_id` predicate *in addition to* RLS, on every query.
- **PostgreSQL FK checks bypass the referencing session's RLS.** Any INSERT establishing a new FK (dataset_id, agent_id, prompt_version_id, run_id, case_id) needs a scoped ownership SELECT first. Array columns (`expected_document_ids`, `expected_product_ids`) have no FK at all and need the same ownership check.
- Primary keys are `app.core.ids.uuid7()`. Money is `Decimal`; `cost_usd` columns are `Numeric(12, 6)`.
- `ruff check .`, `ruff format --check .`, `mypy --strict app/` must pass.
- Conventional Commits ending with exactly:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- **Baseline: 983 backend tests collected, ~300 web tests** on `main` at `bab5049`. Record the exact numbers at the start of Task 1 and keep them passing.
- Never log case questions, answers, reference answers or judge rationale — ids, counts, durations and scores only.

## Review Focus

1. **A case whose turn calls `create_lead`** — no `leads` row, conversation, message or citation may survive the run. Pinned in Task 4.
2. **A prompt version activated while a run is in progress** — every result of that run must still record the pinned version. Pinned in Task 4.
3. **The worker retries a run that crashed halfway** — already-scored cases are not re-run or re-billed; `UNIQUE (run_id, case_id)` never raises. Pinned in Task 4.
4. **A judge that returns garbage or raises** — the judge scorer is `error`, the case fails, the run continues. Pinned in Task 3 and Task 4.
5. **Model-written answer text containing `<script>` or markdown** — rendered as text in the dashboard. Pinned in Task 6.

## Existing interfaces you build on

- `app/chat/service.py` — `ChatService(session, tenant, provider_override=None).send(agent_id, user_text, conversation_id=None, channel=..., override_provider=None, override_model=None) -> AsyncIterator[ChatEvent]`. Events: `ChatMessageStart(conversation_id, message_id, created)`, `ChatTextDelta(text)`, `ChatToolCallStart(calls)`, `ChatToolCallEnd(results: list[ChatToolCallResult(tool_call_id, tool_name, result: excerpt, is_error)])`, `ChatCitations(citations: list[CitationPayload])`, `ChatMessageEnd(usage, cost_usd, latency_ms, model, prompt_version_id)`, `ChatError(code, message)`. It never commits; the caller owns the transaction. It writes `message_tool_calls` rows with the tool's **full** result (`MessageToolCall.result["content"]`).
- `app/rag/retrieve.py` — `CitationPayload` (`chunk_id`, `document_id`, `product_id`, `document_title`, `excerpt`, …), `excerpt()`, `EXCERPT_MAX_CHARS = 240`.
- `app/core/tenancy.py` — `TenantContext`, `tenant_session(tenant)` (commits on exit).
- `app/prompts/service.py` — `PromptService.active_version(prompt_id)`; `app/agents/service.py` — `AgentService.get_agent`, `get_config`.
- `app/llm/base.py` — `LLMProvider.generate_structured(request, schema)`; `app/llm/registry.get_provider(name)`, `provider_is_configured`, `KNOWN_PROVIDERS`; `app/llm/pricing.estimate_cost(model, usage) -> Decimal | None`.
- `app/conversations/service.py` — `ConversationService.record_usage(RecordUsageInput(...))` writes `usage_events` (check its FK-ownership handling for `conversation_id=None`).
- `app/workers/tasks.py`, `settings.py`, `enqueue.py` — arq job shape; `tests/unit/test_worker_settings.py` pins registration.
- `app/api/products.py` — **the commit-then-enqueue REST pattern to copy** (§7 of the spec).
- `app/graphql/resolvers.py`, `types.py`, `context.py` — resolver/service idiom, `_build`, `_require_tenant`, dataloaders.
- `tests/conftest.py` — `tenant_a`, `tenant_b`, `enable_builtin_tool`; `tests/factories.agent_input`; `tests/integration/test_chat_tools.py` is the closest integration idiom.
- Migrations at head `0013_seed_product_tools`. Yours start at `0014_evaluations`.
- Web: **`docs/DESIGN.md` is binding — read it first, update it after.** `lib/format.ts`, `components/ui/`, `components/products/ProductsTable.tsx` and `ProductImports.tsx` (closest table + polling patterns), `components/shell/nav.ts`.

## File Structure

```text
apps/api/app/
├── db/models/evaluation.py        # Task 1 — EvalDataset, EvalCase, EvalRun, EvalResult, EvalRunStatus
├── evaluations/
│   ├── __init__.py
│   ├── schemas.py                 # Task 1 — pydantic inputs
│   ├── service.py                 # Task 1 (CRUD) + Task 4 (runs: create/cancel/claim/record)
│   ├── scorers.py                 # Task 3 — pure, deterministic
│   ├── judge.py                   # Task 3 — LLM-as-judge
│   └── runner.py                  # Task 4 — drives ChatService per case
├── api/evaluations.py             # Task 4 — POST /api/v1/evaluations/runs
├── alembic/versions/0014_evaluations.py   # Task 1
├── chat/service.py                # Task 2 — prompt_version_id pin
├── core/tenancy.py                # Task 2 — rolled_back_tenant_session
├── workers/tasks.py, settings.py  # Task 4 — run_evaluation_task
└── graphql/{types,resolvers}.py   # Task 5
apps/web/src/
├── app/dashboard/evaluations/page.tsx                 # Task 6 — datasets + recent runs
├── app/dashboard/evaluations/[id]/page.tsx            # Task 6 — dataset: cases, start run, runs
├── app/dashboard/evaluations/runs/[runId]/page.tsx    # Task 6 — run results + compare
├── components/evaluations/*.tsx                       # Task 6
└── lib/evaluations.ts                                 # Task 6 — pure helpers (compare, status)
```

---

### Task 1: Schema and dataset/case service

**Files:**
- Create: `app/db/models/evaluation.py`, `app/evaluations/__init__.py`, `app/evaluations/schemas.py`, `app/evaluations/service.py`, `alembic/versions/0014_evaluations.py`
- Modify: `app/db/models/__init__.py`, `tests/integration/test_migrations.py` (extend the parametrized RLS table list — do not add a parallel block)
- Test: `tests/integration/test_evaluation_service.py`

**Interfaces produced:**

```python
class EvalRunStatus(enum.StrEnum):
    PENDING = "pending"; RUNNING = "running"; COMPLETED = "completed"
    FAILED = "failed"; CANCELLED = "cancelled"

# app/evaluations/schemas.py
class CreateDatasetInput(BaseModel):   name: str (1..200, stripped), description: str | None (<=2000)
class UpdateDatasetInput(BaseModel):   name: str | None, description: str | None
class CaseInput(BaseModel):
    question: str                      # 1..4000 chars, stripped
    reference_answer: str | None = None  # <=4000
    required_phrases: list[str] = []   # <=20 items, each 1..200, stripped, deduped
    expected_tool_names: list[str] = []  # each must be a known builtin name (app/db/builtin_tools.py)
    expected_document_ids: list[uuid.UUID] = []   # <=20, deduped
    expected_product_ids: list[uuid.UUID] = []    # <=20, deduped
    tags: list[str] = []               # <=10, each 1..50
    # model_validator: at least one of reference_answer / required_phrases /
    # expected_tool_names / expected_document_ids / expected_product_ids is non-empty
MAX_CASES_PER_DATASET = 200

class EvaluationService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None
    async def list_datasets(self) -> list[EvalDataset]
    async def get_dataset(self, dataset_id) -> EvalDataset          # NotFoundError cross-tenant
    async def create_dataset(self, data: CreateDatasetInput) -> EvalDataset   # duplicate name -> ConflictError (or the codebase's equivalent)
    async def update_dataset(self, dataset_id, data: UpdateDatasetInput) -> EvalDataset
    async def delete_dataset(self, dataset_id) -> None
    async def list_cases(self, dataset_id) -> list[EvalCase]        # ordered by created_at, id
    async def count_cases(self, dataset_id) -> int
    async def create_case(self, dataset_id, data: CaseInput) -> EvalCase   # enforces MAX_CASES_PER_DATASET
    async def update_case(self, case_id, data: CaseInput) -> EvalCase      # full replace
    async def delete_case(self, case_id) -> None
    async def list_runs(self, dataset_id: uuid.UUID | None = None, limit: int = 50) -> list[EvalRun]  # newest first
    async def get_run(self, run_id) -> EvalRun
    async def list_results(self, run_id) -> list[EvalResult]        # ordered by created_at
```

**Requirements:**
- Columns exactly per spec §3. Enum type name `eval_run_status`. `summary`, `scores`, `tool_calls` are `JSONB NOT NULL DEFAULT '{}'`/`'[]'`. Array defaults `'{}'`.
- Indexes: `(organization_id, dataset_id)` on cases; `(organization_id, dataset_id, created_at DESC)` on runs; `UNIQUE (run_id, case_id)` on results; a partial index on runs `WHERE status IN ('pending','running')` on `dataset_id` supporting the one-active-run check.
- `enable_rls` on all four tables. `down_revision = "0013_seed_product_tools"`; one head; `downgrade base` → `upgrade head` round-trips.
- `create_case`/`update_case` validate **every** `expected_document_ids` / `expected_product_ids` entry exists in this organization (`Document.organization_id == tenant.organization_id`, same for `Product`) and raise `ValidationError` naming the count of unknown ids — never which ids exist in another org.
- Unknown `expected_tool_names` → `ValidationError` at the schema layer.

**Tests:** create/list/update/delete dataset; duplicate dataset name rejected; case with no expectation rejected; case referencing another org's document id rejected **and** the error does not echo the id; 201st case rejected; cross-tenant `get_dataset`/`list_cases`/`get_run` → `NotFoundError`, `list_datasets` never shows the other org's; deleting a dataset cascades its cases; RLS entries added to `test_migrations.py`; migration round-trip.

- [ ] Record baselines (`uv run pytest --collect-only -q | tail -1`; `npx vitest run` count in `apps/web`).
- [ ] Write the tests, run them, watch them fail.
- [ ] Implement models, migration, schemas, service; `alembic upgrade head`.
- [ ] Tests pass; ruff/format/mypy clean; commit `feat(evaluations): datasets, cases and the eval schema (Phase 6 Task 1)`.

---

### Task 2: Pin a prompt version per turn, and a rolled-back tenant session

**Files:**
- Modify: `app/chat/service.py` (`send`, `_resolve_system_prompt`), `app/prompts/service.py` (add `get_version`), `app/core/tenancy.py`
- Test: extend `tests/integration/test_chat_service.py`; create `tests/integration/test_rolled_back_session.py`

**Interfaces produced:**

```python
# ChatService.send gains one keyword argument, defaulted, after override_model:
prompt_version_id: uuid.UUID | None = None
# None -> unchanged behaviour (active version, or DEFAULT_SALES_SYSTEM_PROMPT when agent.prompt_id is None).
# Set  -> that version's text and variables are used; ChatMessageEnd.prompt_version_id and the
#         persisted assistant message record it. Must satisfy version.prompt_id == agent.prompt_id,
#         else ValidationError("prompt version does not belong to this agent's prompt"); an agent with
#         no prompt given a version -> the same ValidationError. Resolved BEFORE any row is written,
#         like the provider (a bad pin must not leave a conversation behind).

# PromptService
async def get_version(self, version_id: uuid.UUID) -> PromptVersion   # two-layer scoped; NotFoundError cross-tenant

# app/core/tenancy.py
@asynccontextmanager
async def rolled_back_tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]:
    """Like tenant_session, but the transaction is ALWAYS rolled back on exit, success or
    failure. For work whose writes must not survive -- an evaluation case's chat turn."""
```

**Requirements:**
- `rolled_back_tenant_session` applies `app.current_org_id` exactly as `tenant_session` does, and rolls back on normal exit **and** on exception (the exception still propagates).
- No change to the playground/SSE route; `app/api/chat.py` does not pass the new argument.

**Tests:** a pinned non-active version's text reaches `FakeProvider.last_request.system`, and the persisted message and `ChatMessageEnd` carry its id; a version from a different prompt of the same org → `ValidationError` and no conversation row created; another org's version id → `NotFoundError`; default behaviour unchanged (existing tests stay green); `rolled_back_tenant_session`: a row inserted inside is gone afterwards; RLS is applied inside (a query for another org's rows returns none); an exception inside propagates and still rolls back.

- [ ] Tests first, watch them fail, implement, gates, commit `feat(chat): pin a prompt version per turn; add a rolled-back tenant session (Phase 6 Task 2)`.

---

### Task 3: Scorers and the judge

**Files:**
- Create: `app/evaluations/scorers.py`, `app/evaluations/judge.py`
- Test: `tests/unit/test_eval_scorers.py`, `tests/unit/test_eval_judge.py`

**Interfaces produced:**

```python
# app/evaluations/scorers.py -- no I/O, no DB, no LLM imports
@dataclass(frozen=True, slots=True)
class ObservedToolCall:
    name: str; arguments: dict[str, Any]; is_error: bool; content: str   # full content

@dataclass(frozen=True, slots=True)
class Observation:
    answer: str
    error: str | None                       # ChatError message, or None
    tool_calls: list[ObservedToolCall]
    cited_document_ids: list[uuid.UUID]     # deduped, first-seen order
    cited_product_ids: list[uuid.UUID]

@dataclass(frozen=True, slots=True)
class Expectations:                          # built from an EvalCase
    reference_answer: str | None
    required_phrases: list[str]
    expected_tool_names: list[str]
    expected_document_ids: list[uuid.UUID]
    expected_product_ids: list[uuid.UUID]

@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float | None       # None only when status == "error"
    passed: bool
    status: Literal["scored", "error"]
    detail: dict[str, Any]    # JSON-serialisable: e.g. {"missing": [...], "unexpected": [...]}

def normalize(text: str) -> str
def score_required_phrases(exp: Expectations, obs: Observation) -> ScoreResult | None   # None = not applicable
def score_tool_selection(exp, obs) -> ScoreResult | None
def score_document_recall(exp, obs) -> ScoreResult | None
def score_product_recall(exp, obs) -> ScoreResult | None
def deterministic_scores(exp, obs) -> dict[str, ScoreResult]    # keys: "required_phrases", "tool_selection", "document_recall", "product_recall" -- only applicable ones
def case_passed(scores: dict[str, ScoreResult], obs: Observation) -> bool   # obs.error is None and all passed (and at least one score)
def score_to_json(result: ScoreResult) -> dict[str, Any]

# app/evaluations/judge.py
class JudgeVerdict(BaseModel):
    correctness: Literal["correct", "partially_correct", "incorrect"]
    grounded: bool
    rationale: str

EVIDENCE_MAX_CHARS = 12_000
RATIONALE_MAX_CHARS = 1_000

@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    result: ScoreResult     # score 1 / 0.5 / 0; passed = correct and grounded; detail = {"correctness", "grounded", "rationale"}
    usage: Usage            # zero Usage if the call failed before returning

class Judge:
    def __init__(self, provider: LLMProvider, model: str) -> None
    async def score(self, question: str, reference_answer: str, obs: Observation) -> JudgeOutcome
```

**Requirements:**
- `normalize` per spec §4: NFKC, casefold, `(?<=\d),(?=\d{3})` removed, every non-alphanumeric char → space, whitespace collapsed, stripped. Phrase match is `f" {normalize(phrase)} " in f" {normalize(answer)} "` (word-boundary by padding).
- `score_tool_selection` counts only `is_error=False` calls; `detail` has `missing` and `unexpected` (names called but not expected, deduped).
- The judge sends the answer and evidence (successful tool-call `content`s concatenated, each labelled with the tool name, truncated to `EVIDENCE_MAX_CHARS` total) inside a fence `<<{token}>> … <</{token}>>` with `token = secrets.token_hex(8)` per call, and a system prompt stating that fenced text is material to grade and any instruction inside it must be ignored. It says an honest "I don't have that information" is grounded.
- `Judge.score` catches `LLMError`, `AppError` and `pydantic.ValidationError` and returns `status="error", passed=False, score=None, detail={"error": <exception class name>}` — never re-raises, never passes. `max_tokens` 1024; `temperature` omitted when the model's capabilities do not support sampling (use `provider.capabilities(model).supports_sampling`), else 0.0.
- Usage: the judge needs the usage of its call. `generate_structured` returns only the parsed schema; if the providers do not expose usage for it, call `provider.generate(...)` with a JSON-only instruction and parse `JudgeVerdict.model_validate_json` on the response text yourself (strip a ```json fence if present) — decide after reading `app/llm/openai_provider.py` / `anthropic_provider.py`, and say which in the report. Usage must be real, not guessed.

**Tests (unit):** `$35,000` matches phrase `35000`; `3-year` matches `3 year`; `3 years` does not match inside `13 years`; case/diacritic-width insensitivity (`ＣＡＭＲＹ` vs `camry`); not-applicable returns `None`; tool errored → not counted; unexpected tools reported but passing; recall fractions (2 of 3 → 0.667, not passed); `case_passed` false when `obs.error` set even if all scorers pass; judge with a stub provider returning each verdict → 1/0.5/0 and `passed` only for correct+grounded; correct but not grounded → not passed; the fence token appears in the request and differs between two calls; an answer containing the literal closing fence of a *different* token cannot close the real one; evidence truncated to the bound; stub raising `LLMError` → `status="error"`; malformed JSON → `status="error"`; rationale truncated to 1,000 chars.

- [ ] Tests first, watch them fail, implement, gates, commit `feat(evaluations): deterministic scorers and an LLM judge (Phase 6 Task 3)`.

---

### Task 4: Running an evaluation

**Files:**
- Create: `app/evaluations/runner.py`, `app/api/evaluations.py`, `app/evaluations/queue.py` (typed enqueue wrapper, like `app/rag/queue.py`)
- Modify: `app/evaluations/service.py` (run methods), `app/workers/tasks.py`, `app/workers/settings.py`, `app/main.py` (mount router), `tests/unit/test_worker_settings.py`
- Test: `tests/integration/test_evaluation_runner.py`, `tests/integration/test_evaluations_api.py`

**Interfaces consumed:** Task 1 service/models, Task 2 `send(..., prompt_version_id=)` and `rolled_back_tenant_session`, Task 3 `Observation`, `Expectations`, `deterministic_scores`, `case_passed`, `score_to_json`, `Judge`.

**Interfaces produced:**

```python
# app/evaluations/schemas.py
class StartRunInput(BaseModel):
    dataset_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version_id: uuid.UUID | None = None
    provider: str | None = None; model: str | None = None        # both or neither
    judge_provider: str | None = None; judge_model: str | None = None   # both or neither

# EvaluationService additions
async def create_run(self, data: StartRunInput) -> EvalRun
    # validates per spec §5 (1..200 cases, agent ownership, version belongs to agent.prompt_id,
    # provider/judge_provider in KNOWN_PROVIDERS and provider_is_configured, no pending/running run
    # for the dataset -> ConflictError); pins prompt_version_id (active at start, or None when the
    # agent has no prompt) and provider/model; status=pending, case_count set, triggered_by=tenant.user_id
async def cancel_run(self, run_id) -> EvalRun          # pending/running -> cancelled, finished_at=now; terminal -> unchanged
async def recorded_case_ids(self, run_id) -> set[uuid.UUID]

# app/evaluations/runner.py
async def run_evaluation(
    tenant: TenantContext,
    run_id: uuid.UUID,
    *,
    provider_override: LLMProvider | None = None,   # tests: the agent's provider
    judge_provider_override: LLMProvider | None = None,  # tests: the judge's provider
) -> None

# app/workers/tasks.py
async def run_evaluation_task(ctx: dict[str, Any], *, organization_id: str, evaluation_run_id: str) -> None

# REST
POST /api/v1/evaluations/runs   body: StartRunInput   -> 202 {id, status, case_count, ...}
```

**Requirements:**
- **REST handler:** authenticated (`get_current_tenant`), creates the run inside `tenant_session`, then **after the commit** enqueues `run_evaluation_task` — copy `app/api/products.py`'s ordering and its comment's reasoning. Rate-limit it like other write endpoints if a helper exists (`app/core/rate_limit.py`); otherwise the one-active-run rule is the limit.
- **Registration:** add to `WorkerSettings.functions` via `arq.worker.func(run_evaluation_task, timeout=3600)` (keeping the name arq enqueues by), and pin it in `test_worker_settings.py`.
- **Runner flow, per spec §5:**
  1. In its own `tenant_session`: load run; if not `pending`/`running`, return. Set `running`, `started_at` (if unset). Commit.
  2. Load cases (ids + expectations) and `recorded_case_ids`. For each case not yet recorded, in `created_at` order:
     a. Re-read run status in a short `tenant_session`; if `cancelled`, stop.
     b. In `rolled_back_tenant_session`: drive `ChatService(session, tenant, provider_override=...).send(run.agent_id, case.question, channel=ConversationChannel.API, override_provider=run.provider, override_model=run.model, prompt_version_id=run.prompt_version_id)`. Collect the answer (concatenated deltas), `ChatError`, citations (document/product ids, deduped), `ChatMessageEnd` usage/cost/latency/prompt_version_id. Then, **before leaving the block**, read that message's `MessageToolCall` rows (by `message_id` from `ChatMessageStart`, org-scoped) for full content, arguments and `is_error`; if that read fails, fall back to the `ChatToolCallEnd` excerpts. An `AppError` raised before streaming (e.g. agent deleted) becomes `Observation.error`.
     c. Score: `deterministic_scores`, plus `Judge(...).score(...)` when the run has a judge **and** the case has a `reference_answer`, stored under key `"judge"`.
     d. In a new `tenant_session`: insert the `eval_results` row (tool_calls as `[{name, arguments, is_error, excerpt}]` with `excerpt` from `app.rag.retrieve.excerpt`), write `usage_events` for the turn (agent's provider/model, `conversation_id=None`) and, if the judge ran, a second row for the judge (judge provider/model, `agent_id=run.agent_id`); increment `completed_count`. Use `INSERT … ON CONFLICT (run_id, case_id) DO NOTHING` so a retry race cannot raise. Skip the usage rows when the insert did nothing.
  3. Finally, in a `tenant_session`: if still `running`, compute the summary per spec §5 from **all** the run's results and set `completed`, `finished_at`.
  - Any exception outside a case's own handling: mark the run `failed` with a bounded error (`app.rag.ingest.bounded_error_message`) through an **independent** session, log `evaluation_run_failed`, re-raise (so arq's retry resumes).
  - Log `evaluation_run_started` / `evaluation_case_scored` (run_id, case_id, passed, latency_ms) / `evaluation_run_completed` (counts, pass_rate, duration) — never question or answer text.
- `summary.cost_usd` is the sum of per-case cost (turn + judge) as a string, or `null` if any case's cost was `None`; `errored` counts results with `error` set; `pass_rate = passed / len(results)` (0 results → `null`).
- The per-case `cost_usd` on `eval_results` is turn cost plus judge cost (`None` if either is unpriced).

**Tests (integration, FakeProvider / stub judge):**
- A 3-case dataset runs to `completed` with 3 results, correct `passed` flags and summary numbers, `completed_count == 3`.
- **Review Focus 1:** a case scripted to call `create_lead` (enable it with `enable_builtin_tool`) → after the run, zero `leads`, zero `conversations`, zero `messages` for the org; the result's `tool_calls` shows the successful `create_lead`; `tool_selection` passes for `expected_tool_names=["create_lead"]`.
- `usage_events` has one row per case (plus one per judged case), `conversation_id IS NULL`.
- **Review Focus 2:** activate a different prompt version after `create_run` but before `run_evaluation` → every result's `prompt_version_id` is the pinned one and the provider received the pinned text.
- **Review Focus 3:** pre-insert a result for case 1, run → case 1 not re-sent to the provider (count provider calls), total results 3, no error.
- **Review Focus 4:** judge stub raises on case 2 → case 2 `passed=False` with `scores.judge.status == "error"`, cases 1 and 3 still scored, run `completed`.
- A turn error (FakeProvider that raises an `LLMError` subclass) → that case records `error`, fails, run continues.
- Cancel between cases (cancel from inside a stub provider's first call, via an independent session) → run `cancelled`, fewer results than cases, status not overwritten to `completed`.
- Retrieval: a case with `expected_document_ids=[doc]` where the scripted turn calls `retrieve_knowledge` over a real ingested chunk → `document_recall` passes and `cited_document_ids` contains it.
- Cross-tenant: `run_evaluation(tenant_b, run_of_a)` does nothing to run A (raises `NotFoundError` or returns without writes — assert A's run unchanged and no results).
- API: 202 and the job is enqueued after commit (patch the enqueue wrapper and assert the run row is visible from an independent session at call time); 409 on a second active run; 422 for empty dataset, for >200 cases (insert directly), for a prompt version of another prompt, for provider without judge model; 404 for another org's dataset/agent; 401 unauthenticated.

- [ ] Tests first, watch them fail, implement, gates, commit `feat(evaluations): run datasets against the real chat path (Phase 6 Task 4)`.

---

### Task 5: GraphQL surface

**Files:**
- Modify: `app/graphql/types.py`, `app/graphql/resolvers.py`, `packages/shared/schema.graphql` (regenerate with the repo's existing export command — find it in `Makefile`/`README.md`)
- Test: `tests/integration/test_graphql_evaluations.py`, extend `tests/integration/test_tenant_isolation.py` if it enumerates operations

**Interfaces produced (GraphQL):**

```graphql
enum EvaluationRunStatus { PENDING RUNNING COMPLETED FAILED CANCELLED }

type EvaluationDataset { id name description caseCount createdAt updatedAt latestRun: EvaluationRun }
type EvaluationCase { id question referenceAnswer requiredPhrases expectedToolNames
                      expectedDocumentIds expectedProductIds tags createdAt updatedAt }
type EvaluationScore { name: String! score: Float passed: Boolean! status: String! detail: JSON! }
type EvaluationToolCall { name: String! arguments: JSON! isError: Boolean! excerpt: String! }
type EvaluationResult { id caseId question answer error passed scores: [EvaluationScore!]!
                        toolCalls: [EvaluationToolCall!]! citedDocumentIds citedProductIds
                        promptVersionId latencyMs inputTokens outputTokens costUsd: Decimal createdAt }
type EvaluationRun { id datasetId agentId agentName promptVersionId promptVersion: Int provider model
                     judgeProvider judgeModel status caseCount completedCount summary: JSON
                     error triggeredBy createdAt startedAt finishedAt
                     results: [EvaluationResult!]! }

Query:
  evaluationDatasets: [EvaluationDataset!]!
  evaluationDataset(id: ID!): EvaluationDataset          # null-or-error convention: follow `agent(id)`
  evaluationCases(datasetId: ID!): [EvaluationCase!]!
  evaluationRuns(datasetId: ID, limit: Int = 20): [EvaluationRun!]!
  evaluationRun(id: ID!): EvaluationRun
Mutation:
  createEvaluationDataset(input) / updateEvaluationDataset(id, input) / deleteEvaluationDataset(id): Boolean!
  createEvaluationCase(datasetId, input) / updateEvaluationCase(id, input) / deleteEvaluationCase(id): Boolean!
  cancelEvaluationRun(id): EvaluationRun!
```

Follow the codebase's existing conventions rather than adding scalars: money is a `String` (see the `Decimal` comment near `types.py:313`), and free-form JSON is flattened to JSON text in a `String` (see `types.py:511-519`) — so `costUsd: String`, `detail: String!`, `arguments: String!`, `summary` as typed fields (`passed`, `failed`, `errored`, `passRate: Float`, `costUsd: String`, `meanLatencyMs: Int`) rather than raw JSON. Names above are the intent, not mandated spellings. Regenerate with `cd apps/api && uv run strawberry export-schema app.graphql.schema:schema > ../../packages/shared/schema.graphql` and `cd apps/web && npm run codegen`. `EvaluationScore` rows are ordered: `judge`, `required_phrases`, `tool_selection`, `document_recall`, `product_recall`.

**Requirements:**
- Starting a run is **not** a mutation (spec §7) — say so in a comment where the mutations are defined.
- `caseCount`, `latestRun`, `agentName`, `promptVersion` via dataloaders or one batched query each — no N+1 on the datasets or runs list (add a query-count assertion if the codebase has a helper for it).
- `results` resolves only on `evaluationRun(id)` usage; listing runs must not load every result — resolve lazily per field.
- Inputs go through `_build(...)` so pydantic errors become `invalid_input`.

**Tests:** full CRUD through GraphQL; `evaluationRun` returns results with scores in the stated order; cancel works and is idempotent on a terminal run; every query and mutation with another org's id → not found / empty, and `evaluationDatasets` never lists org B's; unauthenticated → `authentication required` code; `schema.graphql` regenerated and diff-free after regeneration.

- [ ] Tests first, watch them fail, implement, regenerate schema, gates, commit `feat(api): GraphQL surface for evaluations (Phase 6 Task 5)`.

---

### Task 6: The Evaluations dashboard

**Files:**
- Create: `apps/web/src/app/dashboard/evaluations/page.tsx`, `[id]/page.tsx`, `runs/[runId]/page.tsx`; `apps/web/src/components/evaluations/` (`DatasetList.tsx`, `CaseForm.tsx`, `CasesTable.tsx`, `StartRunForm.tsx`, `RunsTable.tsx`, `RunSummary.tsx`, `ResultsTable.tsx`, each with a `.test.tsx`); `apps/web/src/lib/evaluations.ts` (+ test)
- Modify: `apps/web/src/graphql/operations.graphql`, regenerate `generated.ts`; `components/shell/nav.ts` (+ its test); `lib/api.ts` if the REST start call needs a helper; `docs/DESIGN.md`

**Requirements:**
- **Read `docs/DESIGN.md` first; update it after** with any new pattern (score badges, comparison states). Name no colour outside `globals.css`; `conventions.test.ts` enforces it. Reuse `components/ui/*`, `lib/format.ts`.
- Nav: add `{ href: "/dashboard/evaluations", label: "Evaluations", icon: <existing suitable icon or add one to icons.tsx>, state: "live" }` to the "Run" group.
- `/dashboard/evaluations`: dataset list (name, case count, latest run pass rate + status), create-dataset form, empty state explaining what a dataset is for in one sentence.
- `/dashboard/evaluations/[id]`: rename/delete dataset; cases table (question, expectations summarised as chips); add/edit case form — question, reference answer, required phrases (one per line), expected tools (checkboxes of the four builtins), expected documents (multi-select from `documents` query, ready ones), expected products (search via `products(search:)`), tags. Client-side mirrors the server's "at least one expectation" rule with an inline message. Start-run form: agent select, prompt version select (the agent's prompt's versions, active marked, default = active), optional model override via the existing `ModelPicker`, optional judge (provider + model via `ModelPicker`). Shows "N cases × (1 + judge) LLM calls" before starting. Calls `POST /api/v1/evaluations/runs` through the same authenticated fetch helper the upload flows use. Runs table (status, pass rate, prompt version, model, cost, started).
- `/dashboard/evaluations/runs/[runId]`: summary tiles (pass rate, passed/failed/errored, per-scorer means, cost, mean latency), progress bar while running, cancel button while pending/running. Poll while `pending`/`running`, stop when terminal or the tab is hidden — the same pattern as products imports. Results table: question, pass/fail, a badge per score, latency, cost; a row expands to answer, error, judge rationale, tool calls (name, arguments, error flag, excerpt) and cited ids. "Compare with" select listing other completed runs of the dataset → each row gets regressed / improved / unchanged / new, with counts at the top and a filter to show only regressions.
- `lib/evaluations.ts`: `compareRuns(base: ResultLike[], candidate: ResultLike[]): Comparison[]` keyed by `caseId` (a result with `caseId === null` is compared by `question`), `formatPassRate`, `isRunActive(status)`, `runCallEstimate(caseCount, withJudge)`.
- **Answers, questions, rationales and tool arguments are untrusted. Render as text** — no `dangerouslySetInnerHTML`, no markdown pass. Test with a `<script>` payload and a markdown payload (`**bold**` shows literally). (Review Focus 5.)
- Regenerate `packages/shared/schema.graphql` consumers (`generated.ts`) — CI fails on a stale artefact.

**Tests:** `compareRuns` for all four states and the null-`caseId` fallback; nav test updated; `CaseForm` blocks submit without an expectation; `StartRunForm` shows the call estimate and sends the pinned version id; `ResultsTable` renders the `<script>`/markdown payloads literally and expands a row; `RunSummary` shows `—` for a null cost; polling stops on a terminal status.

- [ ] Implement with tests; `npx tsc --noEmit`, `npx eslint .`, `npx vitest run`, `npx next build` clean; commit `feat(web): Evaluations dashboard (Phase 6 Task 6)`.

---

## Final Verification

- [ ] `git fetch origin && git merge origin/main` — resolve conflicts.
- [ ] Full suites: the recorded baselines still passing, plus the new tests.
- [ ] All gates clean; `alembic heads` single; `downgrade base` → `upgrade head` round-trips.
- [ ] `packages/shared/schema.graphql` and `generated.ts` regenerated and committed.
- [ ] `docker compose up -d --wait`, then drive it for real: create a dataset with three cases (one answerable from an uploaded document, one product question expecting `search_products`, one unanswerable with a refusal reference answer), run it with the `fake` provider through the REST endpoint, watch the run page poll to completion, then run it again against a second prompt version and use Compare. If a real provider key is configured, repeat once with a judge.
- [ ] Whole-branch review, one fix wave, one scoped re-review, adjudicate residuals.
- [ ] Update `README.md`'s roadmap row for Phase 6, `ARCHITECTURE.md` (status note at the top, §3.7 pointer to PHASE-6.md §3, a §9.8 "Phase 6 as delivered" table), and `PHASE-6.md` §9 with anything else not delivered.
- [ ] Push and open a PR.
