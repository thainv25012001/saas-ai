# Phase 2 — LLM Chat with Streaming

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). That document is the binding spec;
> this one records the decisions Phase 2 adds on top of it.

**Goal:** a business owner opens the playground, types a question, and watches the answer
stream back — grounded in nothing yet (RAG is Phase 3), but produced by a real LLM through
a provider layer that can be swapped without touching business logic.

```text
Playground (Next.js)
      │  POST /api/v1/chat/stream        (SSE)
      ▼
Chat service
      │  resolve agent → config → ACTIVE prompt version → render system prompt
      │  assemble windowed history
      ▼
LLMProvider  ── openai │ anthropic │ openrouter │ fake
      ▼
stream text deltas → persist message + usage + cost
```

---

## 1. What Phase 2 does and does not include

**In:** the provider abstraction with two real implementations, conversations and messages,
a streaming chat endpoint, per-message token/cost accounting, and a working playground.

**Out:** retrieval, embeddings, tools, citations, the agent orchestrator's multi-step loop,
the public visitor widget. Those are Phases 3–4 and 7. The message schema carries the
columns they will need (`content_blocks`, `prompt_version_id`) so they arrive as additions,
not migrations of live data.

---

## 2. The provider abstraction

### 2.1 Why the internal format is Anthropic-shaped

The normalized representation is a list of **content blocks**, not a flat string:

```python
Message(role="assistant", content=[TextBlock(...), ToolUseBlock(...)])
```

Anthropic's Messages API is natively block-structured; OpenAI's Chat Completions is a
string plus a parallel `tool_calls` array. Modelling on the richer of the two and
**downcasting** for OpenAI means Phase 4's tool calling needs no change to the internal
type — only to the OpenAI adapter. The reverse choice would force a rewrite.

### 2.2 The interface

```python
class LLMProvider(Protocol):
    name: str
    async def generate(self, request: CompletionRequest) -> CompletionResponse: ...
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
    async def generate_structured(
        self, request: CompletionRequest, schema: type[BaseModel]
    ) -> BaseModel: ...
```

`stream` is **not** `async def` — it returns an async iterator directly, so callers write
`async for event in provider.stream(req)` without an extra `await`.

### 2.3 Model capabilities are data, not assumptions

This is the decision Phase 2 turns on, and it is not cosmetic.

**Claude Opus 5, Opus 4.8/4.7 and Sonnet 5 reject `temperature` with a 400.** Sampling
parameters were removed from those models. But `agents.temperature` exists in the Phase 1
schema (default `0.3`) and the dashboard has a temperature input — so a naive adapter that
forwards every agent field would make every Anthropic agent fail on its first message.

The same models also handle reasoning differently: thinking is adaptive and on by default,
depth is controlled by `output_config.effort` rather than by sampling, and `budget_tokens`
is rejected outright.

So each provider declares a **capability record per model** and filters the request against
it. Unsupported parameters are dropped with a debug log, never forwarded:

```python
@dataclass(frozen=True)
class ModelCapabilities:
    supports_sampling: bool      # temperature / top_p
    supports_thinking: bool
    thinking_style: Literal["adaptive", "budget", "none"]
    supports_effort: bool
    max_output_tokens: int
```

The agent's `temperature` therefore means "use it where the model accepts it".

**Deferred to a later phase — recorded rather than quietly dropped.** An earlier draft of this
section promised that the dashboard would show `temperature` as inapplicable when the selected
model ignores it. Phase 2 does not do that: the agent form renders an unconditional numeric
input, and a user editing a `claude-opus-5` agent sees a temperature box whose value is silently
discarded before the request is sent.

The backend half — the capability table, the drop-with-debug-log, and tests asserting the
parameter is absent for models that reject it and present for models that accept it — is built
and covered. What is missing is only the UI's honesty about it, and closing that requires
exposing `ModelCapabilities` over GraphQL: a new query, type, resolver and tests. That is a
task's worth of work for a cosmetic gap, so it is deferred rather than rushed.

Until then the dashboard overstates what it controls. That is a real, if small, way the product
misleads its own operator, and it should be closed before anyone relies on the field.

### 2.4 Provider defaults

| Provider | Default model | Reasoning | Sampling |
|---|---|---|---|
| `anthropic` | `claude-opus-5` | `thinking: {"type": "adaptive"}`, depth via `output_config.effort` | not sent |
| `openai` | `gpt-4o-mini` | n/a | `temperature` sent |
| `fake` | `fake-1` | n/a | echoed back for assertions |

### 2.5 The fake provider is a first-class citizen

`FakeProvider` is production code under `app/llm/`, not a test fixture. It replays a
scripted sequence of events deterministically, with no network.

It exists for three reasons, and the third is the one that matters most: **every test in
this phase runs against it**, so the suite is fast, deterministic, free, and — critically —
the chat pipeline is testable by anyone who clones the repo without holding an API key.
A test suite that only runs for people with billing set up is a test suite that stops
being run.

It is also what `ENVIRONMENT=local` falls back to when no key is configured, so the
playground works out of the box.

---

## 3. Error handling

Provider failures are normalized into the existing `AppError` hierarchy before they reach
any caller, so the SSE layer never has to know which SDK raised:

| Provider condition | Domain error | Surfaced as |
|---|---|---|
| Rate limited | `LLMRateLimitError` | `rate_limited`, retry advised |
| Timeout / connection lost | `LLMUnavailableError` | `llm_unavailable` |
| Auth rejected (bad/missing key) | `LLMConfigurationError` | `llm_misconfigured` — an operator problem, not a user one |
| Model refused or returned nothing | `LLMEmptyResponseError` | `llm_empty_response` |

Mid-stream failures are the awkward case: some tokens have already reached the browser.
The stream emits a terminal `error` event and the partial assistant message **is persisted**
with `error` set — losing it would make the conversation history disagree with what the user
saw on screen.

---

## 4. Streaming transport

`POST /api/v1/chat/stream` returns `text/event-stream`, per spec §2.2. Event envelope:

```jsonc
{"type": "message_start",  "conversation_id": "...", "message_id": "..."}
{"type": "text_delta",     "text": "Based on"}
{"type": "message_end",    "usage": {"input_tokens": 412, "output_tokens": 88},
                           "cost_usd": "0.00123", "latency_ms": 1840,
                           "model": "gpt-4o-mini", "prompt_version_id": "..."}
{"type": "error",          "code": "llm_unavailable", "message": "..."}
```

Headers that matter and are easy to omit: `Cache-Control: no-cache`,
`X-Accel-Buffering: no`, and no compression on this route — without them a proxy buffers
the whole response and streaming silently degrades to a single delayed blob.

A heartbeat comment (`: ping`) every 15 s keeps idle connections from being reaped.

---

## 5. Cost accounting

Every assistant message records `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`,
`model`, `provider` and `prompt_version_id`, and writes a `usage_events` row.

Pricing lives in `app/llm/pricing.py` as a per-model table in **USD per million tokens**,
with cost computed in `Decimal` — not float. Money in floats is how you get
`0.30000000000000004` in a billing column. An unknown model logs a warning and records
`cost_usd = NULL` rather than guessing, so a missing price is visible instead of silently
wrong.

**Known gap: usage lost on a mid-stream client disconnect.** The `usage_events` write
above happens inside the same DB transaction as the rest of the turn (see `ChatService.send`
and `app/api/chat.py`). If the client disconnects after the LLM call has already completed
(the provider has been paid) but before that transaction commits, the whole turn — including
the usage write — is rolled back rather than committed, per §4's chat-streaming design: an
in-flight request whose client vanished has no channel left to report a partial success
through, so the transaction is torn down rather than left in an ambiguous state. The
organization is never billed for a call that already happened; the SSE layer logs a warning
with the model, token counts, and cost at the point this is discarded, but nothing currently
reconciles it. Fixing this for real means recording usage in a write that survives the
request's own transaction being rolled back — e.g. writing `usage_events` immediately after
the provider call returns, outside the conversation's transaction — which is real work that
belongs with billing (Phase 7), not a small fix here.

---

## 6. Conversation history

The provider receives: rendered system prompt, then the last `history_window` turns
(config, default 20), then the new user message. Older turns are dropped for now;
summarization arrives when conversations get long enough to need it, which the playground
alone will not produce.

`prompt_version_id` is stamped on every assistant message, so any answer can be traced to
the exact prompt text that produced it. That is what makes Phase 5's evaluation possible.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| No API key in this environment | `FakeProvider` covers the whole pipeline; real providers are unit-tested against mocked SDK clients. Live verification needs the user's key. |
| Sending `temperature` to a model that rejects it | Capability table + a test per provider asserting the parameter is dropped. |
| SSE buffered by a proxy | Headers documented and set; noted in the README as a deployment prerequisite. |
| Cost drift as prices change | Pricing table is one file with a `last_verified` date; unknown models fail visibly. |
| A provider SDK's stream shape changing | Each adapter has a conformance test running the same scenarios, so a break localizes to one file. |
