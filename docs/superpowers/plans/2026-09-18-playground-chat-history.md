# Playground Chat History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the playground list an agent's past conversations, reopen one, and continue it.

**Architecture:** Expose the already-persisted `conversations`/`messages` rows through two new GraphQL queries (batching citations with a DataLoader, the pattern `Context` already uses for agent configs and chunk counts); title each playground conversation with a background arq job that falls back to the first user message; add a conversation list to the playground that loads a transcript back into the existing `TurnState`.

**Tech Stack:** FastAPI + strawberry-graphql + SQLAlchemy async + arq/Redis (api), Next.js + urql + vitest (web).

**Spec:** [`docs/superpowers/specs/2026-09-18-playground-chat-history-design.md`](../specs/2026-09-18-playground-chat-history-design.md)

## Global Constraints

- Cross-tenant reads raise `NotFoundError`, never `PermissionDeniedError` — a distinguishable error confirms the row exists.
- `conversation(id)` resolves to `null` for a missing/foreign id (matches `document(id)`); `agent(id)` style errors are not used here.
- Titles are generated **only** for `ConversationChannel.PLAYGROUND`.
- The title job runs only when `title IS NULL`. This is the only guard against re-billing.
- A generated title is untrusted text: truncate to 255 before the write, render via JSX interpolation only.
- Host-run api tests need `REDIS_URL=redis://localhost:6379/0` (the `.env` uses docker hostnames).
- Run `make schema` after any GraphQL type change, then `npm run codegen` in `apps/web`.

---

### Task 1: `list_for_agent` gains channel, limit, offset

**Files:**
- Modify: `apps/api/app/conversations/service.py:61-71`
- Test: `apps/api/tests/integration/test_conversation_service.py`

**Interfaces:**
- Produces: `ConversationService.list_for_agent(agent_id, *, channel: ConversationChannel | None = None, limit: int | None = None, offset: int = 0) -> list[Conversation]`

- [ ] **Step 1: Write the failing tests**

```python
async def test_list_for_agent_filters_by_channel(...):
    # two conversations on one agent, one PLAYGROUND one API
    rows = await service.list_for_agent(agent_id, channel=ConversationChannel.PLAYGROUND)
    assert [r.id for r in rows] == [playground_id]

async def test_list_for_agent_orders_by_last_activity(...):
    # A conversation whose first turn failed has last_message_at IS NULL.
    # ORDER BY last_message_at DESC drops it to the bottom or off the list
    # depending on NULL ordering; COALESCE with created_at keeps it in place.
    rows = await service.list_for_agent(agent_id)
    assert [r.id for r in rows] == [newest_id, untouched_id, oldest_id]

async def test_list_for_agent_paginates(...):
    rows = await service.list_for_agent(agent_id, limit=1, offset=1)
    assert [r.id for r in rows] == [second_newest_id]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && REDIS_URL=redis://localhost:6379/0 uv run pytest tests/integration/test_conversation_service.py -k list_for_agent -v`
Expected: FAIL — `TypeError: list_for_agent() got an unexpected keyword argument 'channel'`

- [ ] **Step 3: Implement**

```python
async def list_for_agent(
    self,
    agent_id: uuid.UUID,
    *,
    channel: ConversationChannel | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Conversation]:
    stmt = select(Conversation).where(
        Conversation.agent_id == agent_id,
        Conversation.organization_id == self.tenant.organization_id,
    )
    if channel is not None:
        stmt = stmt.where(Conversation.channel == channel)
    # COALESCE, not `last_message_at DESC`: a conversation whose first turn
    # failed before any message was appended has last_message_at IS NULL and
    # would sort to the bottom (or off a limited page) though it is the most
    # recent thing the user did.
    stmt = stmt.order_by(
        func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
    ).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Verify pass, then run the whole conversations suite**
- [ ] **Step 5: Commit** — `feat(api): filter and paginate an agent's conversations`

---

### Task 2: GraphQL `Conversation` type and `conversations` query

**Files:**
- Modify: `apps/api/app/graphql/types.py`, `apps/api/app/graphql/resolvers.py`
- Test: `apps/api/tests/integration/test_graphql_conversations.py`

**Interfaces:**
- Consumes: Task 1's `list_for_agent` signature.
- Produces: `gql.Conversation` (fields `id, agent_id, channel, status, title, last_message_at, created_at`), `gql.ConversationChannel`, `gql.ConversationStatus`, `_conversations(info) -> ConversationService`, `Query.conversations(agent_id, channel, limit=20, offset=0)`.

- [ ] **Step 1: Write the failing tests** — a conversation is listed with its fields; the channel filter reaches the service; another organization's agent returns an empty list, not an error.
- [ ] **Step 2: Run to verify failure** — `Cannot query field 'conversations' on type 'Query'`
- [ ] **Step 3: Implement** the enums, `Conversation.from_model`, `_conversations(info)` (copying the `_documents` helper exactly), and the resolver.
- [ ] **Step 4: Verify pass**
- [ ] **Step 5:** `make schema` and commit — `feat(api): expose an agent's conversations over GraphQL`

---

### Task 3: `conversation(id)` with its messages, citations batched

**Files:**
- Modify: `apps/api/app/graphql/types.py`, `apps/api/app/graphql/context.py`, `apps/api/app/graphql/resolvers.py`
- Test: `apps/api/tests/integration/test_graphql_conversations.py`

**Interfaces:**
- Produces: `gql.Message` (`id, seq, role, content, provider, model, input_tokens, output_tokens, cost_usd, latency_ms, finish_reason, error, created_at`, plus a `citations` field), `gql.MessageCitation`, `Context.citation_loader: DataLoader[uuid.UUID, list[MessageCitation]]`, `Query.conversation(id) -> gql.Conversation | None`.

- [ ] **Step 1: Write the failing tests**
  - a conversation returns its messages oldest-first with cost/model metadata intact;
  - a message with `error` set and `content` NULL is returned, not skipped;
  - **citations for a multi-message conversation are fetched in one query** — assert by counting `SELECT ... FROM message_citations` statements via a SQLAlchemy `before_cursor_execute` event, not by reading the code;
  - another organization's conversation id resolves to `null`.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — `_load_citations` on `Context` mirroring `_load_chunk_counts` (including the explicit `organization_id` predicate: Layer 1 admits no exceptions), and `Conversation.messages` resolving through `ConversationService.history(id)` with **no limit**.
- [ ] **Step 4: Verify pass**
- [ ] **Step 5:** `make schema` and commit — `feat(api): read a conversation's full transcript`

---

### Task 4: Title text rules (pure functions)

**Files:**
- Create: `apps/api/app/conversations/titles.py`
- Test: `apps/api/tests/unit/test_conversation_titles.py`

**Interfaces:**
- Produces: `TITLE_MAX_LENGTH = 255`, `FALLBACK_MAX_LENGTH = 120`, `clean_title(raw: str) -> str | None`, `fallback_title(first_user_message: str) -> str`, `build_title_messages(user_text: str, assistant_text: str) -> list[LLMMessage]`, `TITLE_SYSTEM_PROMPT`, `TITLE_MAX_TOKENS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_clean_title_rejects_whitespace_only():
    assert clean_title("   \n ") is None

def test_clean_title_strips_surrounding_quotes():
    # Models habitually answer a "give me a title" prompt with a quoted string.
    assert clean_title('"Pricing for the starter plan"') == "Pricing for the starter plan"

def test_clean_title_truncates_to_the_column_bound():
    assert len(clean_title("x" * 400)) == TITLE_MAX_LENGTH

def test_fallback_title_truncates_on_a_word_boundary():
    assert fallback_title("word " * 40).endswith("word") 
    assert len(fallback_title("word " * 40)) <= FALLBACK_MAX_LENGTH

def test_fallback_title_keeps_a_short_message_whole():
    assert fallback_title("How much is the starter plan?") == "How much is the starter plan?"
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: app.conversations.titles`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `feat(api): title text rules for conversations`

---

### Task 5: The `title_conversation_task` arq job

**Files:**
- Modify: `apps/api/app/workers/tasks.py`, `apps/api/app/workers/settings.py`
- Create: `apps/api/app/conversations/queue.py`
- Test: `apps/api/tests/integration/test_title_conversation_task.py`, `apps/api/tests/unit/test_worker_settings.py`

**Interfaces:**
- Consumes: Task 4's `clean_title`/`fallback_title`/`build_title_messages`.
- Produces: `title_conversation_task(ctx, *, organization_id: str, conversation_id: str) -> None`, `enqueue_title(conversation_id, organization_id) -> None`.

- [ ] **Step 1: Write the failing tests**
  - success: a `FakeProvider` scripted with `"Pricing questions"` leaves `conversations.title == "Pricing questions"`;
  - **already titled makes no LLM call at all** — a provider whose `generate` raises if called; this is the test that guards the bill;
  - provider failure writes the truncated first user message instead, and leaves `title` non-NULL so the job cannot recur;
  - a conversation with no messages writes nothing;
  - `test_worker_settings.py` pins the new function in `WorkerSettings.functions`.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — open its own `tenant_session` end to end like `ingest_document_task`; resolve the agent's provider via `get_provider(agent.provider)`; enqueue helper copied from `rag/queue.py` including the `aclose()` note and the `__name__`-not-a-literal job name.
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `feat(api): title a conversation in the background`

---

### Task 6: Enqueue the title after the turn commits

**Files:**
- Modify: `apps/api/app/api/chat.py`, `apps/api/app/chat/service.py`
- Test: `apps/api/tests/integration/test_chat_endpoint.py`

**Interfaces:**
- Consumes: Task 5's `enqueue_title`.

- [ ] **Step 1: Write the failing tests**
  - a first playground turn enqueues exactly one title job, **after** `session_cm.__aexit__` — assert ordering by recording both events into one list;
  - a turn that fails before `message_end` enqueues nothing;
  - a second turn in the same conversation enqueues nothing;
  - a non-playground channel enqueues nothing.
- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** — `ChatService.send()` records whether this turn created the conversation; `_stream_body`'s `finally` enqueues after the session closes cleanly. Monkeypatch `chat_api.enqueue_title` in tests, mirroring how `documents_api.enqueue_ingest` is patched.
- [ ] **Step 4: Verify pass, then the full api suite**
- [ ] **Step 5: Commit** — `feat(api): queue a title once a conversation's first turn commits`

---

### Task 7: Web — operations, codegen, and transcript conversion

**Files:**
- Modify: `apps/web/src/graphql/operations.graphql`
- Create: `apps/web/src/lib/conversation-transcript.ts`
- Test: `apps/web/src/lib/conversation-transcript.test.ts`

**Interfaces:**
- Produces: `ConversationsDocument`, `ConversationDocument` (generated), `toTranscript(messages) -> ChatMessageData[]`, `conversationLabel(conversation) -> string`.

- [ ] **Step 1: Write the failing tests**

```ts
it("renders a failed turn as an error message, not a blank one", () => {
  // content is NULL for a turn that died before producing text.
  const [message] = toTranscript([{ ...base, role: "ASSISTANT", content: null, error: "llm_unavailable" }]);
  expect(message.status).toBe("error");
  expect(message.text).toBe("");
});

it("carries model, cost and latency onto the message meta", () => { ... });

it("falls back to the first user message when the title has not landed yet", () => {
  // The title job is asynchronous; a brand-new row is title: null and the
  // list must still read as something rather than a blank row.
  expect(conversationLabel({ title: null, firstUserMessage: "How much is the starter plan?" }))
    .toBe("How much is the starter plan?");
});
```

- [ ] **Step 2: Run to verify failure** — `cd apps/web && npx vitest run src/lib/conversation-transcript.test.ts`
- [ ] **Step 3: Add the operations, run `npm run codegen`, implement**
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `feat(web): convert a stored conversation into a transcript`

---

### Task 8: Web — the playground conversation list

**Files:**
- Modify: `apps/web/src/app/dashboard/playground/page.tsx`

**Interfaces:**
- Consumes: Task 7's `toTranscript`, `conversationLabel`; existing `TurnState`, `sessionTotals`, `useStickToBottom`.

- [ ] **Step 1: Implement** the sidebar — list for the selected agent filtered to `PLAYGROUND`; selecting one loads `conversation(id)`, replaces `messages`, sets `TurnState` to `{ conversationId, committed: true }` so the next send continues the thread, and calls `stickToBottom()`; refetch the list when a turn ends; no polling for titles.
- [ ] **Step 2: Verify** — `npx tsc --noEmit && npx eslint && npx vitest run && npx next build`
- [ ] **Step 3:** Rebuild containers (`docker compose up -d --build api web`) and exercise it end to end.
- [ ] **Step 4: Commit** — `feat(web): reopen a past conversation from the playground`

---

## Self-Review

**Spec coverage:** §3 → Tasks 1-3. §4 → Task 3. §5 → Tasks 4-6. §6 → Tasks 7-8. §7 → tests inside each task. §8/§9 are scope and risk, no task.

**Type consistency:** `list_for_agent(channel=, limit=, offset=)` is used with those exact keywords in Tasks 1-2; `enqueue_title(conversation_id, organization_id)` matches between Tasks 5 and 6; `toTranscript`/`conversationLabel` match between Tasks 7 and 8.
