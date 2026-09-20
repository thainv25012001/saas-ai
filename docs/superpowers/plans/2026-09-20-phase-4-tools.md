# Phase 4 — Tool calling and the agent loop: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the assistant decides when to look something up, when to act, when it has enough, and when to ask — a real multi-step agent loop with native provider tool calling.

**Spec:** [docs/PHASE-4.md](../../PHASE-4.md), extending [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) §5, §7 and §3.6.

## Global Constraints

- Python **3.12**, `uv`; API code under `apps/api/app/`. **GNU `make` is NOT available** — run underlying commands directly.
- Async tests use **`anyio` only**; `pytest-asyncio` is deliberately absent. Use `pytestmark = pytest.mark.anyio`.
- The suite runs with `filterwarnings = ["error"]` — any `DeprecationWarning` fails it.
- **No test may make a network call.** Every test uses `FakeProvider`, `HashingEmbedder`, or a mocked SDK client. A test needing an API key is a defect.
- **Running the suite from the host needs these three overrides** — `.env` addresses the compose network, which does not resolve from the host, and connecting as `postgres` silently bypasses RLS, making every isolation test pass vacuously:
  ```sh
  export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai"
  export MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai"
  export REDIS_URL="redis://localhost:6379/0"
  ```
  Each Bash call is a fresh shell, so re-export every time. Start infra with `docker compose up -d --wait db redis`.
- Tenant-owned tables get `organization_id UUID NOT NULL` + RLS via `enable_rls(op, table)`. Never inline policy SQL.
- **Two-layer tenancy is mandatory** (§2.3): an explicit `organization_id` predicate *in addition to* RLS, on every query. Phase 3 shipped one query relying on RLS alone and it was ruled a defect.
- **PostgreSQL FK checks bypass the referencing session's RLS.** Any INSERT establishing a new FK must be preceded by a scoped ownership SELECT.
- Primary keys are UUIDv7 from `app.core.ids.uuid7()`. Money is `Decimal`.
- `ruff check .`, `ruff format --check .`, `mypy --strict app/` must pass; the suite stays warning-free.
- Conventional Commits ending with exactly:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Baseline: 619 backend tests, 229 web tests** on `main`.

## Existing interfaces you build on

- `app/llm/types.py` — `TextBlock`, **`ToolUseBlock`**, **`ToolResultBlock`**, `Message`, **`ToolSpec`**, `CompletionRequest` (which **already has a `tools` field no provider reads**), `CompletionResponse`, and the `StreamEvent` union (`MessageStartEvent | TextDeltaEvent | UsageEvent | MessageEndEvent | ErrorEvent` — **no tool event yet**). Phase 2 built these deliberately so the message format would not change when tools arrived.
- `app/llm/base.py` — `LLMProvider` Protocol (`capabilities`, `generate`, `stream`, `generate_structured`), `ModelCapabilities`.
- `app/llm/` — `anthropic_provider.py`, `openai_provider.py`, `openrouter_provider.py`, `fake_provider.py`, `registry.py`, `pricing.py`, `errors.py`, `catalog.py`.
- `app/chat/service.py` — `ChatService(session, tenant, provider_override=None)`, `send(...)` yielding `ChatMessageStart` / `ChatCitations` / `ChatTextDelta` / `ChatMessageEnd` / `ChatError`. Retrieval currently runs unconditionally, inside a savepoint, gated on the org having a `ready` document.
- `app/rag/retrieve.py` — `RetrievalService(session, tenant, embedder=None).retrieve(query, *, top_k, candidates, min_score) -> list[RetrievedChunk]`. Hybrid vector + full-text fused with RRF; both arms carry explicit `organization_id` predicates.
- `app/conversations/service.py` — `ConversationService`: `create`, `get`, `history`, `next_seq`, `append_message`, `record_usage`.
- `app/documents/service.py`, `app/db/models/document.py`, `app/db/models/citation.py`.
- `app/core/` — `config.Settings`, `tenancy` (`TenantContext`, `tenant_session`), `errors` (`AppError` + subclasses), `ids.uuid7`, `logging.get_logger`, `rate_limit.enforce_rate_limit`, `redis.get_redis`.
- `app/api/chat.py` — the SSE endpoint, its event envelope, the pump-task/bounded-queue/heartbeat machinery, and the `exc_info` threading that decides commit vs rollback. **Read it whole before adding an event.**
- Migrations at head `0007_message_citations`. Yours start at `0008_tools_and_leads`.
- Web: `apps/web/src/lib/sse.ts` (event parsing + `fetchWithRefresh`), `components/chat/ChatMessage.tsx`, `app/dashboard/playground/page.tsx`, `components/ui/`. `docs/DESIGN.md` is binding for UI work — read it first, update it after.

## File Structure

```text
apps/api/app/
├── tools/
│   ├── base.py            # Task 1 — AgentTool, ToolContext, ToolResult
│   ├── registry.py        # Task 1 — specs_for / execute
│   ├── retrieve.py        # Task 5 — retrieve_knowledge
│   └── leads.py           # Task 6 — create_lead
├── agents/runner.py       # Task 4 — the loop
├── db/models/tool.py      # Task 3 — Tool, AgentTool, MessageToolCall
├── db/models/lead.py      # Task 3 — Lead
└── leads/service.py       # Task 6
apps/web/src/components/chat/ToolCall.tsx      # Task 8
apps/web/src/app/dashboard/leads/page.tsx      # Task 8
```

---

### Task 1: The tool abstraction and registry

**Files:** create `app/tools/__init__.py`, `base.py`, `registry.py`; test `tests/unit/test_tool_registry.py`.

**Interfaces produced:**
- `ToolContext` (pydantic): `organization_id`, `agent_id`, `conversation_id`, `request_id`, `visitor_id: str | None`.
- `ToolResult` (pydantic): `content: str`, `data: dict | None = None`, `citations: list[CitationRef] = []`, `is_error: bool = False`.
- `AgentTool` ABC: `name: str`, `description: str`, `args_model: type[BaseModel]`, `timeout_seconds: float = 10.0`, `async def execute(self, args, ctx) -> ToolResult`.
- `ToolRegistry`: `register(tool)`, `specs_for(names: list[str]) -> list[ToolSpec]`, `async execute(call: ToolUseBlock, ctx) -> ToolResult`.

**Requirements:**
- `specs_for` derives each JSON schema from `args_model.model_json_schema()`. Do not hand-write schemas — they would drift from the validation that actually runs.
- `execute` parses `call.input` through `args_model`. **A `ValidationError` becomes a `ToolResult(is_error=True)` the model can read and correct from, never an exception** — §7.3. Include the field errors in `content`.
- `execute` enforces `timeout_seconds` with `asyncio.timeout`; a timeout is `is_error=True` with a plain message, not a hung request.
- An unknown tool name is `is_error=True`, not a `KeyError` — a model can hallucinate a tool name and that must not end the turn.
- `ToolContext.organization_id` is documented as server-derived and never from model output.

**Tests:** schema generation matches the Pydantic model including required fields; invalid args yield `is_error` with the field named; a tool exceeding its timeout yields `is_error` and does not hang the test; an unknown name yields `is_error`; a successful call returns the tool's own `ToolResult`; `specs_for` returns only the names asked for.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 2: Provider tool calling, and a scriptable fake

**Files:** modify `app/llm/types.py`, `base.py`, `anthropic_provider.py`, `openai_provider.py`, `openrouter_provider.py`, `fake_provider.py`; test `tests/unit/test_tool_streaming.py`, extend the three provider test modules.

**Interfaces produced:**
- `ToolUseEvent(type="tool_use", block: ToolUseBlock)` added to the `StreamEvent` union.
- `CompletionResponse.stop_reason` already exists; providers must set it to the tool-use value when the model asks for a tool.
- `FakeProvider(script: list[FakeTurn])` where `FakeTurn` is either text or a list of tool calls, played back one turn per `stream()` call.

**Requirements:**
- All three real providers must **send** `request.tools` (today the field exists and is ignored) and **emit** `ToolUseEvent` as tool calls arrive.
- Anthropic streams `tool_use` as a content block with `input_json_delta` fragments that must be accumulated and parsed once complete. OpenAI streams a `tool_calls` array with index-keyed argument fragments. **Both are partial-JSON accumulation problems and both need a test with the arguments split across at least three chunks** — a fake that delivers whole JSON in one delta tests nothing about the accumulator.
- `_NO_TEXT_EXPECTED_STOP_REASONS` already covers `tool_use` / `tool_calls`; confirm a tool-only response does not trip the "no text" warning path.
- `FakeProvider` keeps its current text-only construction working — every existing chat test uses it and none may change.

**Tests:** each provider sends tool specs in its request payload (assert on the mocked client's call args); each emits `ToolUseEvent` with correctly accumulated arguments from fragmented deltas; a tool-only turn sets the right `stop_reason`; the fake plays a two-turn script in order; the fake still works text-only.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 3: Schema for tools, tool calls and leads

**Files:** create `app/db/models/tool.py`, `app/db/models/lead.py`, `alembic/versions/0008_tools_and_leads.py`; modify `app/db/models/__init__.py`; test `tests/integration/test_tool_schema.py`, extend `tests/integration/test_migrations.py`.

**Interfaces produced:** `Tool`, `AgentToolLink`, `MessageToolCall`, `Lead` per §3.6, plus `ToolType` and `LeadStatus` enums.

**Requirements:**
- `down_revision = "0007_message_citations"`. Exactly one head afterwards; verify a `downgrade base` → `upgrade head` round-trip.
- `enable_rls` on every new tenant-owned table. `tools.organization_id` is **nullable** — a null means a global builtin — so its RLS policy must admit `organization_id IS NULL` as well as the current org. **Get this wrong and either builtins are invisible to everyone, or the policy leaks.** Test both halves.
- `agent_tools` is `PRIMARY KEY (agent_id, tool_id)`; it also carries `organization_id` for the two-layer predicate.
- `MessageToolCall`: `message_id`, `tool_call_id`, `tool_name`, `arguments jsonb`, `result jsonb`, `is_error`, `error_message`, `duration_ms`.
- Add the new tables to the parametrized RLS test in `test_migrations.py` — extend the existing list, do not add a parallel block.

**Tests:** RLS on each new table; a global builtin (`organization_id IS NULL`) is visible to two different orgs while an org-scoped tool is visible to only one; cross-tenant reads return nothing; the migration round-trips.

- [ ] Tests first, watch them fail, implement, migrate, gates, commit.

---

### Task 4: The agent loop

**Files:** create `app/agents/runner.py`; test `tests/unit/test_agent_loop.py`.

**Interfaces produced:**
- `AgentRunner(provider, registry, *, max_steps: int)` with
  `async def run(self, system: str, messages: list[Message], tool_names: list[str], ctx: ToolContext) -> AsyncIterator[AgentEvent]`.
- `AgentEvent` union: `AgentTextDelta`, `AgentToolCallStart(calls)`, `AgentToolCallEnd(results)`, `AgentUsage`, `AgentStepLimit`.

**Requirements — the three load-bearing properties from §4 of the spec:**
- **Termination.** The loop runs at most `max_steps`; exhausting it emits `AgentStepLimit` rather than stopping silently. Follow §5.1's `for ... else`.
- **Isolation.** Calls within a step run under `asyncio.gather(..., return_exceptions=True)` or equivalent; one failure must not abort its siblings, and **every issued `tool_use` must get a matching `tool_result`** — a missing one is a protocol error real providers reject.
- **Failure is not fiction.** A tool exception becomes `is_error=True` with a message; it is never swallowed into an empty result.
- Accumulated usage across steps is summed, not overwritten — a multi-step turn that reports one step's tokens under-bills.

**Tests (each must be shown red under a mutation of the behaviour it claims to test):**
1. No tool calls → one step, text streamed, loop exits.
2. One tool call then text → exactly two steps, and the tool's result appears in the messages handed to the provider on step two (inspect the captured request, not the fake's call count).
3. Two calls in one step run in parallel and both results return; assert on both, with distinct results so one cannot stand in for the other.
4. One of two parallel calls raises → the other still returns, and both produce `tool_result` blocks.
5. A model that asks for a tool on every step hits `max_steps` and emits `AgentStepLimit`.
6. Usage sums across steps.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 5: `retrieve_knowledge`, and two Phase 3 debts

**Files:** create `app/tools/retrieve.py`; modify `app/rag/retrieve.py`; test `tests/integration/test_retrieve_tool.py`, extend `tests/integration/test_retrieve.py`.

**Requirements:**
- `RetrieveKnowledgeTool`: args `query: str`, `top_k: int = 5` (clamped). Returns ranked chunk text in `content` with citation refs in `citations`.
- **An empty result returns an explicit "no relevant knowledge found" message**, never an empty string or an empty list — §5.2 names this as what stops the model papering over ignorance. Test it.
- **Debt 1 — `documents.status`.** Add `AND d.status = 'ready'` to both retrieval arms. Phase 3 reproduced live that a document the dashboard shows as `failed` still grounds answers, and carried it here explicitly. Test: a document with stale chunks whose latest ingest failed does not surface.
- **Debt 2 — bare negation.** A query parsing to a bare negation (`-cat` → `!'cat'`) matches every chunk through the strict keyword arm, which has no rank floor, so it can cite the whole corpus. Fix on the strict arm. Test with a leading-hyphen query.

**Tests:** a relevant query returns the right chunk with citations; an irrelevant query returns the explicit not-found result; `top_k` is clamped; cross-tenant isolation via `ToolContext.organization_id`; both debts above.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 6: `create_lead`

**Files:** create `app/tools/leads.py`, `app/leads/service.py`, `app/leads/schemas.py`; test `tests/integration/test_create_lead_tool.py`.

**Requirements:**
- Args: `name: str`, `email: EmailStr | None`, `phone: str | None`, `interest: str`. **At least one of email/phone required**, enforced by a model validator — that requirement is what makes the agent ask a follow-up (§5.2), so it belongs in the schema, not in prose.
- A validation failure is a `ToolResult(is_error=True)` naming the missing field, not an exception.
- Rate-limited per conversation via the existing `enforce_rate_limit`; exceeding it is `is_error=True`, not a 429 that ends the turn.
- The write goes through a two-layer-scoped service, with the scoped ownership SELECT on `conversation_id` before the insert (FK checks bypass RLS).
- Phone/email normalised before storage.

**Tests:** a valid call creates the row with the right org; neither contact method → `is_error` naming both; a malformed email → `is_error`, no row; the rate limit trips to `is_error` and writes nothing; cross-tenant `conversation_id` is refused and writes nothing.

- [ ] Tests first, watch them fail, implement, gates, commit.

---

### Task 7: Wire the loop into chat and SSE

**Files:** modify `app/chat/service.py`, `app/api/chat.py`, `app/conversations/service.py`; test `tests/integration/test_chat_tools.py`, extend `tests/integration/test_chat_endpoint.py`.

**Requirements:**
- `ChatService.send` runs the agent loop instead of a single completion. **Retrieval stops running unconditionally** — remove the prefix path and the readiness gate; `retrieve_knowledge` replaces both.
- New SSE events `tool_call_start` and `tool_call_end` carrying tool name, arguments, a compact result and `is_error`. **Never the full tool result payload** — the same reason citations carry an excerpt.
- Persist one `MessageToolCall` per call, in the same transaction as the assistant message, after the scoped ownership check on `message_id`.
- `message_citations` continues to be written, now from the tool's returned citations.
- **Every existing chat test must still pass.** Where a test asserted the old unconditional-retrieval behaviour, changing it is expected — but say so explicitly in the report, and confirm each change still asserts what its name claims.
- Read `app/api/chat.py` whole before adding events: a previous phase wedged this endpoint with a seam bug that per-task review missed. The pump/queue/heartbeat/`exc_info` machinery must be untouched.

**Tests:** a knowledge question triggers the tool and the answer cites; a greeting triggers **no** tool call and no citations; `tool_call_start` precedes `tool_call_end`; a failing tool surfaces `is_error` and the turn still completes; `message_tool_calls` rows persist with arguments and result; cross-tenant isolation end to end.

- [ ] Tests first, watch them fail, implement, migrate if needed, gates, commit.

---

### Task 8: The web surface

**Files:** create `components/chat/ToolCall.tsx`, `app/dashboard/leads/page.tsx`; modify `lib/sse.ts`, `components/chat/ChatMessage.tsx`, `app/dashboard/playground/page.tsx`, the GraphQL documents, `packages/shared/schema.graphql`, `docs/DESIGN.md`.

**Requirements:**
- **Read `docs/DESIGN.md` first and update it after** — it is binding for UI work. Name no colour outside `globals.css`; `conventions.test.ts` enforces it.
- Parse the two new SSE events in `sse.ts` with the same strict narrowing the existing events use; a malformed event is dropped, not rendered.
- Render a tool call inline in the transcript: name, a readable argument summary, and a collapsed result. A failed call reads as failed, not as an empty success.
- **Tool arguments and results are model- and document-derived. Render as text, never markup** — no `dangerouslySetInnerHTML`, no markdown pass. Test with a `<script>` payload.
- Leads page: list with status, linked conversation, created time. Read-only this phase.
- Regenerate `packages/shared/schema.graphql` — CI fails on a stale artefact.

**Tests:** vitest + testing-library, matching the existing style. Tool call renders name and args; an errored call is visibly distinct; a `<script>` payload renders as text; the leads list renders and its empty state shows.

- [ ] Implement, `tsc`/`eslint`/`vitest`/`build`, gates, commit.

---

## Final Verification

- [ ] `git fetch origin && git merge origin/main` — resolve conflicts.
- [ ] Full suite: **619 backend / 229 web baseline** still passing, plus the new ones.
- [ ] `ruff check .`, `ruff format --check .`, `mypy --strict app/`, `tsc`, `eslint`, `build` clean.
- [ ] `alembic heads` single; `downgrade base` → `upgrade head` round-trips.
- [ ] `packages/shared/schema.graphql` regenerated and committed.
- [ ] `docker compose up -d --wait` and drive a real turn: ask a knowledge question, see the tool call and a cited answer; say "hi", see no tool call. This is the only check that proves the model actually decides.
- [ ] Whole-branch review, one fix wave, one scoped re-review, adjudicate residuals.
- [ ] Update `ARCHITECTURE.md` §9 and the README; record in `PHASE-4.md` anything not delivered.
- [ ] Push and open a PR.
