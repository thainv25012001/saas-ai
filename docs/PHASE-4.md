# Phase 4 — Tool calling and the agent loop

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (§5 agent, §7 tool calling, §3.6 tools
> and leads). That document is the binding spec; this one records what Phase 4 adds.

**Goal:** the assistant stops being a one-shot answerer and becomes an agent — it decides
when to look something up, when to act, when it has enough, and when to ask.

```text
                      ┌───────────────────────────────┐
user turn ──▶ loop ──▶│ stream → text? tool calls?    │
                      └───────────────────────────────┘
                          │no calls          │calls
                          ▼                  ▼
                        done          execute in parallel
                                             │
                                      results ──▶ back into messages
                                             │
                                    (up to max_agent_steps)
```

---

## 1. Scope

**In:** the tool abstraction and registry, provider tool-calling for all three providers,
the multi-step loop with a step cap, `retrieve_knowledge`, `create_lead`, the
`message_tool_calls` record, tool events over SSE, and the playground rendering them.

**Out, and deliberately:** `search_products` and `get_product`, with the whole
`products` table behind them (§3.4). Those are not one tool each — they are a second
knowledge subsystem with its own embedding, its own hybrid search, and its own import
path. Folding them in here would double the phase and bury the thing this phase exists to
prove. **Phase 5.**

Also out: MCP (§8), evaluation (§3.7), and history compaction (§5.3) — the last only
because the existing last-N-turns window is not yet the constraint.

---

## 2. Retrieval becomes a tool

This is the change Phase 3 named and deferred, and it is the point of the phase.

Today every chat turn retrieves, unconditionally, before the model sees anything. That was
the right half to build first — it made grounding demonstrable — but it means the agent
pays retrieval on "hi", and irrelevant chunks enter the prompt on every turn. §5.2 calls
injecting unneeded context a real hallucination source, and Phase 3's own review found the
system citing up to five sources for `"can you write me a poem about the sea"` until a
relevance floor was added. A floor treats the symptom. Not retrieving is the cure.

So `retrieve_knowledge` becomes a tool the model chooses to call. The `RetrievalService`
built in Phase 3 is unchanged underneath; what changes is who decides to call it.

**A debt this repays for free.** Phase 3 parked query rewriting — resolving "what about the
SE?" against the previous turns — as a planning miss. Making retrieval a tool largely
dissolves it: the model formulates the `query` argument itself, with the conversation in
front of it, so it writes "Camry SE trim features" rather than passing the user's pronoun
through verbatim. That is not a complete substitute for a rewriting step, and it is worse
when the model is weak, but it is the same mechanism most production agents rely on and it
removes the need for a separate component here.

**What it costs.** One extra round trip on knowledge questions: the model must emit a tool
call, receive results, then answer. Phase 3's unconditional prefix paid zero round trips
and always retrieved. Net, this is cheaper on conversational turns and more expensive on
knowledge turns, and better on both.

---

## 3. The testing problem, which is the same shape as last phase's

Phase 3's hard constraint was that no embeddings API was reachable, and the important
consequence was that a *random* embedding fake would make every retrieval test vacuous.

Phase 4 has the analogous problem one level up. **No real LLM provider is reachable in this
environment** — the tests run against `FakeProvider`, which today replays a canned script of
text. A loop that runs "until the model stops asking for tools" cannot be tested against a
fake that can never ask for a tool.

So `FakeProvider` has to become **scriptable in tool calls, not just text**: a test declares
a sequence of turns — turn one emits a `tool_use` block, turn two emits text — and the fake
plays them back in order. That makes every branch of the loop reachable offline: a single
step, two steps, parallel calls in one step, a tool that errors, and the step cap.

This is not a shortcut around testing the real providers. Their tool-calling translation —
Anthropic's `tool_use` content blocks against OpenAI's `tool_calls` array — is tested
against mocked SDK clients, exactly as Phase 2 tested streaming. What the fake buys is the
*loop*, which is provider-independent and is where the actual logic lives.

**The trap to avoid**, and it is the Phase 3 lesson restated: a scripted fake makes it easy
to write a loop test that passes because the script says so rather than because the loop
did the right thing. A test asserting "two steps happened" must fail if the loop stops at
one; a test asserting a tool result reached the model must inspect what the provider was
actually handed, not that the fake was called.

---

## 4. The loop

§5.1 gives it. Three properties are load-bearing and each gets a test that can fail:

- **It terminates.** `max_agent_steps` (default 5, from `agent_configs`) bounds the worst
  case, and exhausting it is an explicit `step_limit_reached` error, not a silent stop. The
  `for ... else` in §5.1 is doing real work: the `else` fires only when the loop was never
  broken out of.
- **Parallel calls are isolated.** Calls in one step run with `asyncio.gather`, exceptions
  captured per call. One failing tool must not abort its siblings, and the model must
  receive a result for every call it made — a missing `tool_result` for an issued
  `tool_use` is a protocol error that providers reject.
- **Failure never becomes fiction.** A failed tool returns `is_error=True` with a plain
  message. The model is told the tool failed; it is not handed an empty result it can
  paper over. This is the brief's explicit Error Handling requirement.

**Tenancy.** `ToolContext.organization_id` is derived from the authenticated request, never
from model output. There is no argument through which a model can ask for another tenant's
data — and the tools go through the same two-layer-scoped services Phase 3 built, so even a
confused tool body cannot reach across.

---

## 5. `create_lead`, the only write

The first tool in this system that changes data on a visitor's say-so, so it gets the
treatment §7.3 describes: Pydantic validation with at least one contact method required,
a per-conversation rate limit, and a prompt rule requiring the model to confirm details
first.

Requiring `name` plus one of `email`/`phone` at the *schema* level is also what makes the
agent ask a follow-up (§5.2). The model cannot call the tool without them, so "what's your
email?" falls out of the tool definition rather than out of hope.

**Validation errors are results, not exceptions.** A model that passes a malformed email
gets a tool result saying so and can correct it on the next step. A 500 would end the turn.

---

## 6. Carried from Phase 3

Three debts, each recorded there as a Phase 4 prerequisite:

| Debt | Resolution here |
|---|---|
| Retrieval does not filter `documents.status` — a document the dashboard shows as `failed` can still ground answers | Add `AND d.status = 'ready'` to both retrieval queries. Phase 3's review reproduced it live and carried it here explicitly. |
| A query beginning with `-` parses to a bare negation and matches the whole corpus through the strict keyword arm, which has no rank floor | Fix on the strict arm. Low reachability, but the fix is small and the tool now lets a model pass arbitrary text as `query`, which raises it. |
| Query rewriting specified but never built | Largely dissolved by §2 — the model writes the query. Recorded as such rather than left looking forgotten. |

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| The model never calls `retrieve_knowledge` and answers from parametric memory | The prompt requires grounding for company-specific questions, and `retrieve_knowledge` returning an explicit "nothing relevant found" is what stops a silent fallback. Evaluation (Phase 6) is what would actually measure it; until then this is asserted, not proven. |
| A scripted fake makes loop tests that cannot fail | Stated in §3; every loop test must be shown red under a mutation of the behaviour it claims to test |
| The step cap hides a prompt problem | `step_limit_reached` is surfaced, logged and visible in the UI rather than swallowed |
| Tool latency stacks — five steps of 10s each is a 50s turn | Per-tool timeout (default 10s), and the step cap bounds the total. Streaming means the user sees progress throughout. |
| `create_lead` abused to spam | Per-conversation rate limit, and it is the only write tool |
| Tool arguments are model output reaching a real service | Pydantic validation at the boundary; tenancy never comes from arguments; every tool body uses the existing two-layer-scoped services |
