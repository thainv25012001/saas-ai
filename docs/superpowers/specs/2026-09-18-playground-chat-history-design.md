# Playground Chat History — Design

**Goal:** From the playground, see the conversations you have already had with an agent,
reopen any of them, and carry on talking in that same thread.

**Scope:** A read surface for conversations and messages in the GraphQL schema, an
LLM-written title per conversation generated in the background, and a conversation list
in the playground. No change to how a turn is sent, streamed, or persisted.

**Approach in one line:** Expose what the database already holds, give each conversation
a readable label, and let the playground load one back.

---

## 1. Current state

Conversations and messages are persisted in full. Nothing can read them back.

**The data is all there.** `ChatService.send()` writes a `Conversation` row on the first
turn and a `Message` row per turn, and each assistant message carries `provider`,
`model`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `finish_reason` and
`error`. Retrieved chunks are written to `message_citations`. `conversations.last_message_at`
is updated on every append
([`conversations/service.py:143`](../../../apps/api/app/conversations/service.py#L143)).

**The service layer can already read it.** `ConversationService` has `get`,
`list_for_agent` and `history` — written for `ChatService`'s own use, but read methods
all the same.

**Nothing exposes it.** There is no `Conversation` or `Message` type in
[`graphql/types.py`](../../../apps/api/app/graphql/types.py), no query in
[`graphql/resolvers.py`](../../../apps/api/app/graphql/resolvers.py), and no REST route.
The playground therefore keeps its transcript in React state alone: a page reload, an
agent switch, or "New conversation" discards it, while the server keeps every word.

**Two gaps in what exists:**

- `list_for_agent(agent_id)` takes no limit, no offset and no channel filter. It returns
  *every* conversation an agent has ever had, in one list. That is fine for its one
  current caller (nothing calls it) and wrong for a sidebar.
- `conversations.title` is declared (`String(255)`, nullable) and **never written by
  anything**. Every conversation in the database has `title IS NULL`, so a list has no
  label to show.

---

## 2. Approaches considered

**REST endpoints for conversations.** Rejected. The dashboard reads everything over
GraphQL; `/api/v1/chat/stream` is REST only because SSE cannot be a GraphQL field. A
REST conversations API would be a second read idiom for no gain, and the web app would
need a second data-fetching path beside urql.

**Title the conversation synchronously at the end of the first turn.** Rejected. It puts
an LLM call on the chat request's critical path — inside the same transaction that holds
the assistant message, on a connection the streaming body still owns — so a slow or
failing title call delays or endangers the turn the user actually asked for.

**Title from the first user message, no LLM.** Cheaper and simpler, and it remains the
fallback below. Rejected as the primary because the user chose generated titles: "Pricing
for the starter plan" reads better in a list than the first 60 characters of whatever
they typed.

**Chosen: GraphQL read surface + background arq title job + playground sidebar.** Each
piece follows a pattern the codebase already has — `documents(status, limit, offset)` for
a paginated query, `ingest_document_task` for a background job.

---

## 3. GraphQL read surface

Two queries, mirroring the shape of `documents`/`document`:

```graphql
conversations(agentId: UUID!, channel: ConversationChannel, limit: Int = 20, offset: Int = 0): [Conversation!]!
conversation(id: UUID!): Conversation
```

`Conversation` carries `id`, `agentId`, `channel`, `status`, `title`, `lastMessageAt`,
`createdAt`, and its `messages`. `Message` carries `id`, `seq`, `role`, `content`,
`provider`, `model`, `inputTokens`, `outputTokens`, `costUsd`, `latencyMs`,
`finishReason`, `error`, `createdAt`, and its `citations` — everything the playground
already renders for a live turn, so a reopened transcript looks identical to one that
just streamed.

**Nullability follows the existing split.** `conversation(id)` is nullable like
`document(id)`: for the dashboard, "not yours" and "does not exist" are both "nothing to
show". Underneath, `ConversationService.get` raises `NotFoundError` for a cross-tenant id
rather than `PermissionDeniedError`, and that must not change — a distinguishable error
confirms the row exists.

**`list_for_agent` grows three parameters:** `channel`, `limit`, `offset`, ordered by
`COALESCE(last_message_at, created_at) DESC` — not `last_message_at DESC`, which sorts a
conversation whose first turn failed before any message was appended to the bottom of the
list or off it entirely, depending on the database's NULL ordering. Existing behaviour
with no arguments stays the default so nothing else shifts.

`Message.content` is nullable in the schema and stays nullable here: a turn that failed
before producing text has a row with `error` set and `content` empty, and the transcript
renders exactly that.

---

## 4. Reopening a transcript

`history(conversation_id)` already returns messages oldest-first in `seq` order, which is
the order the transcript renders in. Two details decide whether this is correct or merely
looks correct:

**Citations must be loaded in one query.** A transcript is fetched per conversation, not
per message. Resolving `Message.citations` field-by-field would issue one query per
message — a 40-message conversation becomes 40 round-trips. The resolver loads every
citation for the conversation's messages in a single `WHERE message_id IN (...)` and
groups them in Python.

**The whole transcript, not the context window.** `history`'s `limit` exists to bound
what is sent to the model as context. Reopening a conversation passes no limit: the user
is reading their own history, not paying for it as tokens.

---

## 5. Conversation titles

A new arq job beside `ingest_document_task`, registered in
[`workers/settings.py`](../../../apps/api/app/workers/settings.py) by function identity
as that module documents.

**Model:** the agent's own `provider` and `model`. It is the one pair guaranteed to have
a working API key, and it needs no new setting.

**What is sent:** the conversation's first user message and first assistant reply, asked
for a short noun phrase, with `max_tokens` small enough that the call cannot become
expensive by accident. Not the whole transcript — the job runs after the first turn, when
the whole transcript *is* those two messages, and sending more later would make the cost
of a title scale with the length of the conversation.

**When it runs:** enqueued after the first turn of a conversation commits.

**The commit ordering is the load-bearing part.** A chat turn's transaction is committed
by `session_cm.__aexit__` in `_stream_body`'s `finally`
([`api/chat.py`](../../../apps/api/app/api/chat.py)), *after* `ChatService.send()` has
returned. A job enqueued from inside the service could therefore run against a
conversation whose messages are not committed yet and title an empty conversation. The
enqueue happens after `__aexit__` succeeds, matching what the upload endpoint already
does — `enqueue_ingest` is called outside the `tenant_session` block, not inside it
([`api/documents.py:340`](../../../apps/api/app/api/documents.py#L340)). The job also
re-checks that the conversation has messages, so a lost race is a retry rather than a
wrong title.

**Idempotent and bounded.** The job runs only when `title IS NULL`. That single condition
does the work of three guards: a retry does not re-bill, the second turn of a conversation
does not re-title it, and a fallback title written after a failure stops the job from ever
running again for that conversation.

**The fallback is not optional.** On any failure — provider error, empty output, a title
that is only whitespace — the job stores the first user message, truncated to 120
characters on a word boundary, instead. A list row with no label is worse than a plain
one, and a job that keeps failing must still terminate. Because the fallback is a real
write, it also satisfies `title IS NULL` above and stops the job recurring.

**The title is untrusted text.** It is generated *from* whatever the user typed, so two
rules apply. It is truncated to the column's 255 characters before the write, and it is
rendered through JSX text interpolation only — never `dangerouslySetInnerHTML`, never a
markdown pass — exactly the rule `document_title` already carries in
[`sse.ts`](../../../apps/web/src/lib/sse.ts). Prompt injection in the title prompt is
bounded by consequence, not prevented: the worst outcome is a misleading label on the
user's own conversation, in their own organization.

**Only `PLAYGROUND` conversations are titled**, as a module constant a later phase can
widen. Titling every channel means paying for a summary of every customer conversation
once the embedded widget ships — real money for labels nothing currently reads.

---

## 6. Playground UI

A conversation list for the selected agent, beside the transcript. Each row shows its
title and when it was last active; the current conversation is marked.

**Selecting a conversation** loads it through `conversation(id)`, replaces the transcript,
and sets the turn state to *committed* with that id — so the next message continues that
thread instead of opening a new one. This is the existing `TurnState` from
[`chat-turn.ts`](../../../apps/web/src/lib/chat-turn.ts); a loaded conversation is by
definition one the server has committed a turn in, which is precisely what that type
records.

**Costs come free.** `sessionTotals` sums per-message metadata, and loaded messages carry
the same metadata a streamed one does — so reopening a conversation shows what that whole
conversation actually cost.

**The list refetches when a turn ends** — that is when a new conversation appears in it,
and when an existing one moves to the top. It does **not** poll waiting for a title: the
job is asynchronous, so a brand-new row will usually show its fallback label (the first
user message, truncated client-side) and pick up the generated title on the next refetch.
Polling for a label that is already readable would be network traffic bought for a
cosmetic upgrade, and the row is never blank either way.

**Interaction with the model override:** the picker stays seeded from the agent, not from
whatever model answered the conversation being read. Each message already displays the
model that produced it, so history stays accurate without the header pretending the
override is still in effect.

---

## 7. Testing

**Service:** channel filter, limit/offset, ordering by `last_message_at`; cross-tenant
`get`/`history` raise `NotFoundError`.

**Resolvers:** both queries return what they claim; `conversation(id)` for another
organization resolves to `null`, not an error that confirms existence; citations for a
multi-message conversation are fetched in one query, asserted by counting statements, not
by reading the code.

**Title job:** success writes the generated title; provider failure writes the truncated
first message; an already-titled conversation makes **no** LLM call at all; a conversation
with no committed messages does not produce a title.

**Enqueue ordering:** the job is enqueued only after the turn's transaction has committed,
and not at all for a turn that failed before `message_end`.

**Web:** converting an API message into the transcript's `ChatMessageData` (including
error and citation rows), and that selecting a conversation produces a committed
`TurnState` so the next send continues it.

---

## 8. Out of scope (YAGNI)

- Renaming or deleting a conversation from the dashboard.
- Searching conversations, or filtering by date or cost.
- A conversations view outside the playground (an inbox across agents). The `channel`
  argument and the generic types added here are what that view would be built from, but
  it is its own feature with its own questions.
- Backfilling titles for conversations that already exist. They keep `title IS NULL` and
  fall back to their first message in the list; a backfill would bill for summaries of
  test conversations nobody will reopen.
- Cursor pagination. `limit`/`offset` matches `documents` and is correct at this scale.

---

## 9. Risks

**Cost.** One extra LLM call per playground conversation, on the agent's own model. The
`title IS NULL` guard is what keeps it to one; a bug that drops that guard bills per turn
instead of per conversation, so the "already titled makes no call" test is the one that
matters most.

**A slow or dead worker.** Titles arrive late or not at all. The list must render a
conversation whose title is still `NULL` — falling back to its first message client-side —
rather than showing a blank row or waiting.

**Transcript size.** Loading an entire conversation is unbounded by design (§4). A very
long conversation makes a large response. Acceptable for a playground where conversations
are short and the alternative is truncating a user's own history; worth revisiting if the
inbox in §8 ever ships.
