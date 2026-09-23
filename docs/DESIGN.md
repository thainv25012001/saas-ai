# Design system

This records the system the web app already uses. It is not a proposal — every
token, primitive and rule below is in `apps/web/src` today, and most are
enforced by tests. Read it before adding a component, so the next one does not
become the exception.

Scope: `apps/web`. The API has no UI.

## The one rule

**Nothing outside `globals.css` names a colour.** Components reach for semantic
utilities (`bg-surface`, `text-ink-muted`, `border-danger-line`), never
`bg-white` or `text-slate-600`. That is what makes a theme change a one-file
change, and `src/app/globals.test.ts` fails if a token a page relies on stops
compiling.

Everything else here follows from it: if a value can drift between two
components, it belongs in one place that both read.

## Browser storage

`localStorage` holds **UI preferences only** — is a panel collapsed, which tab
was open. Never data, never anything the server is the authority on, and never
a credential: `lib/auth.tsx` keeps the access token in memory precisely because
any injected script can read storage, and that reasoning does not extend to
whether a sidebar is open.

Two rules, both in
[`lib/use-remembered-flag.ts`](../apps/web/src/lib/use-remembered-flag.ts),
which is the one place that touches it:

- **Every access is wrapped in `try`/`catch`.** In a private window or with
  site data blocked, reading *throws* rather than returning null — an unguarded
  read takes the page down.
- **Apply the stored value in an effect, never as initial state.** The server
  has no `localStorage`, so reading it during render makes the first client
  render disagree with the server's and React reports a hydration mismatch. The
  cost is that a remembered non-default paints once in its default state.

`useRememberedFlag(key, computeDefault)` does both. `computeDefault` runs only
when nothing has been stored, and runs on the client, so it may read the
viewport.

## Tokens

Defined in [`apps/web/src/app/globals.css`](../apps/web/src/app/globals.css)
under `@theme`. The values transcribe the Tailwind ramps the UI already used —
this is a restructure, not a rebrand, and `globals.test.ts` compiles both sides
and compares them so the transcription cannot drift.

### Surfaces, back to front

| Token | Utility | Used for | Tailwind |
|---|---|---|---|
| `--color-canvas` | `bg-canvas` | app background | slate-50 |
| `--color-surface` | `bg-surface` | cards, sidebar, controls | white |
| `--color-surface-muted` | `bg-surface-muted` | transcript, secondary hover | slate-100 |

### Lines

| Token | Utility | Used for | Tailwind |
|---|---|---|---|
| `--color-line` | `border-line` | card borders, dividers | slate-200 |
| `--color-line-strong` | `border-line-strong` | control borders | slate-300 |

Controls carry the stronger line so an input is legible as an input against a
card that already has a border.

### Ink

| Token | Utility | Used for | Tailwind |
|---|---|---|---|
| `--color-ink` | `text-ink` | headings, body, values | slate-900 |
| `--color-ink-muted` | `text-ink-muted` | secondary text, descriptions | slate-600 |
| `--color-ink-subtle` | `text-ink-subtle` | meta, placeholders, hints | slate-500 |

### Primary action

`--color-primary` / `--color-primary-hover` / `--color-primary-ink` — slate-900
on white. The primary button is deliberately ink, not a brand hue; there is no
brand colour in this system yet.

### Status tones

Four tones, each with an ink, a surface and a line, so a tone can be a badge, an
alert or a border without inventing values:

| Tone | Means | Tailwind |
|---|---|---|
| `danger` | failed, destructive | red-700 / red-50 / red-200 |
| `success` | saved, healthy | green-700 / green-50 / green-200 |
| `warn` | configured but not live (e.g. a DRAFT agent) | amber-700 / amber-50 / amber-200 |
| `info` | neutral notice | blue-700 / blue-50 / blue-200 |

`warn` is not a failure. A draft agent is warn because it is not answering yet,
not because anything went wrong.

### Radius

| Token | Utility | Used for |
|---|---|---|
| `--radius-control` (0.5rem) | `rounded-control` | inputs, buttons, badges, alerts |
| `--radius-card` (0.75rem) | `rounded-card` | cards, panels, icon tiles |

Two radii. A control is never card-rounded.

### Type

`--font-sans` and `--font-mono` are set on `@theme`; no web font is loaded.
The scale in practice is small and deliberate: `text-sm` for body and controls,
`text-xs` for descriptions and meta, `text-sm font-semibold` for card titles.
Identifiers — slugs, model ids — use `font-mono text-xs`.

## Shared invariants

Two constants exist so components cannot drift — `focusRing` in
[`ui/cn.ts`](../apps/web/src/components/ui/cn.ts) and `controlClasses` in
[`ui/Input.tsx`](../apps/web/src/components/ui/Input.tsx):

- **`focusRing`** — the one focus ring. Nowhere else hand-rolls a
  `focus-visible:` treatment. The ring *offset* stays out of the constant,
  because it legitimately varies (Button `offset-2`, controls `offset-1`) and
  `cn` only concatenates — two offset classes in one call would silently
  resolve to whichever Tailwind emits later.
- **`controlClasses`** — one border, one radius, one focus ring for all three
  of `Input`, `Textarea` and `Select`. If a text field and a dropdown ever look
  like different controls, this is the thing that was bypassed.

`cn()` is the whole of what `clsx` would give us here. The repo carries no UI
dependencies, by design.

## Primitives

All in [`apps/web/src/components/ui/`](../apps/web/src/components/ui/). Reach
for one of these before writing markup.

| Component | Use it for | Notes |
|---|---|---|
| `Button` / `ButtonLink` | actions | `primary` \| `secondary` \| `danger`; `loading` + `loadingLabel` swaps the label. Both share `buttonClasses` so the link and the button cannot diverge. |
| `Card` + `CardHeader` / `CardBody` / `CardFooter` | every panel | `tone="danger"` for destructive sections. Put the submit in `CardFooter`: a confirmation there does not shift the form under the cursor the way an inserted banner does. |
| `Field` | every labelled control | Owns the generated id, `aria-describedby`, `aria-invalid` and the required marker. Takes a render prop: `{(control) => <Input {...control} />}`. Never hand-wire a `<label htmlFor>`. |
| `Input` / `Textarea` / `Select` | text, long text, choice | See Dropdowns below. |
| `Alert` | a message about the page | `danger` gets `role="alert"` (interrupts a screen reader); `success`, `info` and `warn` get `role="status"` (waits its turn). A save confirmation is not worth an interruption; a failure is — but so is a real, named non-fatal outcome (`step_limit_reached`), which is exactly what `warn` is for. |
| `Badge` | a small status fact | `neutral` \| `success` \| `warn` \| `info`. |
| `EmptyState` | a list with nothing in it | Icon, title, description, one action. |
| `PageHeader` | the top of a page | Title, breadcrumb, meta, actions. |
| `Spinner` / `LoadingState` | waiting | `LoadingState` is the one way this app says it is waiting. |
| `Icon` | all iconography | One 24px stroked set in `icons.tsx`, `aria-hidden` by default because every icon here sits beside its own label. An icon-only button therefore carries its own `aria-label`. |

Domain components that compose these live beside their feature —
`components/agents/`, `components/chat/`, `components/shell/` — not in `ui/`.

## Dropdowns

Every dropdown in the app is `ui/Select`. There is no second one.

`Select` sets `appearance-none` and draws its own `chevronDown` in
`text-ink-subtle`, rather than letting the OS paint one. The native arrow sits
flush at the right edge in a colour no token controls and a size `text-sm` does
not reach — which made a `Select` read as a different control from the `Input`
beside it. The chevron is `pointer-events-none` so it cannot swallow the click
meant to open the select.

Rules for options:

- **Label, don't leak.** Options show display names, not wire values —
  `Active`, not `ACTIVE`; `OpenAI`, not `openai`. The mapping lives in `src/lib`
  (`agentStatusLabel`, `providerLabel`), never inline in a page, and the stored
  value is always the raw id.
- **An unknown id still renders.** `providerLabel` falls back to the raw id, so
  a provider the API adds later shows as something rather than blank.
- **Never display a value the form is not holding.** A native `<select>` whose
  `value` matches no option paints its *first* option instead — so an unset
  field shows a real choice while saving nothing. When the value is empty,
  render a selected, `disabled` placeholder option.
- **A blank box must explain itself.** Loading, empty and failed are three
  different states and get three different messages. `ModelPicker` is the
  worked example: `Loading models…`, `No models for this provider`, and on a
  failed fetch a free-text input so the form stays saveable.
- **Long labels get a `title`.** The closed control clips; hover is the only way
  back to the full value.

## Full-bleed screens

Most pages sit in the centred content well and scroll as a whole, with
`PageHeader` at the top. The playground does not: it is in
`FULL_BLEED_ROUTES` (`app/dashboard/layout.tsx`), fills the frame, and manages
its own scrolling. `PageHeader` does not apply there — it carries `mb-6` and
assumes a scrolling page.

What replaces it is a **toolbar**: one wrapping row, `border-b border-line
bg-surface px-6 py-2.5`, holding only what the screen is configured *by*. Three
rules, learned by breaking all of them:

- **Group by question.** The playground's toolbar answers what is answering
  (Agent), what it answers with (Model), and what that costs. Three groups, not
  seven controls in a row.
- **The page title is `sr-only`.** The nav already says which page this is;
  repeating it in the toolbar spends horizontal space to say nothing. The `h1`
  stays for document structure.
- **An action belongs beside what it acts on.** "New conversation" acts on the
  thread, so it lives in the conversation panel, not in a toolbar about models.

A full-bleed screen's own `h1` being `sr-only` is the only place in this app
where a heading is hidden, and it is deliberate.

## Collapsible panels

[`components/chat/ConversationPanel.tsx`](../apps/web/src/components/chat/ConversationPanel.tsx)
is the worked example.

- **Collapse to a rail, not to nothing.** A 3rem rail keeps the way back
  visible and keeps the panel's primary action one click away. A panel that
  vanishes has to put its toggle somewhere else, which is how a toolbar grows
  an eighth item.
- **Both states live in one component.** Expanded and collapsed are two
  renderings of one thing; separating them is how they drift.
- **Collapsed by default under `lg`.** Below that the app's own nav is already
  a drawer, and a 15rem panel beside the content leaves neither usable.
- **The choice is remembered**, per Browser storage above.
- **The toggle says what it does.** `aria-expanded`, plus an `aria-label` and a
  `title` that name the outcome (`Show conversations`), not the icon.

## Polling

Some state changes on the server with nothing to push it — a document being
chunked and embedded by a worker. The Knowledge page polls for it, and
[`lib/use-document-polling.ts`](../apps/web/src/lib/use-document-polling.ts) is
the worked example. Polling, not a websocket: this is a handful of rows and a
3s interval, and a socket would be more moving parts for the same answer.

- **Stop when there is nothing left to learn.** The hook polls only while some
  row is in a non-terminal state. A loop that keeps asking after every document
  is `ready` or `failed` is pure load with no possible new answer.
- **Stop when the tab is hidden.** Nobody is reading it, and a backgrounded tab
  polling every 3s for an afternoon is the version of this bug that nobody
  notices.
- **The interval is the hook's to clear.** Returning the cleanup from the effect
  is the whole contract — a leaked `setInterval` survives the unmount and keeps
  hitting the API for the life of the page.
- **Test the timer, not just the predicate.** A predicate saying "stop" is not
  the same as an interval that stopped. This hook's three conditions each have
  their own fake-timer test asserting the call count stops rising; before they
  existed, deleting the cleanup left the entire suite green.

The predicate lives apart from the effect (`shouldPollDocuments`) so it can be
tested as a pure function, and the effect's callback is memoised so a re-render
does not tear down and re-arm the interval.

The Products page (Phase 5 Task 6) is the second consumer, polling its import
records while one is queued or running. It does not copy the effect: the
interval lives in `usePollWhile(active, onPoll)` in the same file, which
`useDocumentPolling` itself calls, so the fake-timer tests above cover both
pages. Each list brings only its own predicate (`shouldPollImports` in
[`lib/product-status.ts`](../apps/web/src/lib/product-status.ts)), and
`useTabHidden` moved to `lib/use-tab-hidden.ts` so neither page wires the
visibility listener itself. What a settled import *changes* — the catalogue --
is re-read when the set of settled imports changes, not on every tick, which
also catches an import that finished before the first poll saw it running.

## Status as a tone

A lifecycle column (`pending` → `processing` → `ready` | `failed`) becomes a
`Badge` through one lookup — see
[`lib/document-status.ts`](../apps/web/src/lib/document-status.ts). Keep the
mapping in one place rather than inline in the table: the table, the retry
button's enabled state and the polling predicate all read the same status, and
inline `status === "FAILED"` checks in three components are how they disagree.

A terminal state is not automatically a *good* state. A document that processed
with nothing extractable is `ready` with zero chunks, and the label says so
rather than showing a green badge beside a document the assistant cannot use.

A second worked example, added for leads (Phase 4 Task 8):
[`lib/lead-status.ts`](../apps/web/src/lib/lead-status.ts). Same one-lookup
shape, but note it does *not* reuse `document-status.ts`'s tone assignments —
a lead's `warn` means "an in-progress state, not a settled one"
(`QUALIFIED`), which is a different reason from a DRAFT agent's `warn`
("configured but not live") even though both draw on the same token. The
tone's *meaning* is local to what it is describing; only the four tones
themselves, and the rule that a status becomes exactly one of them, are
shared.

A third, for products (Phase 5 Task 6):
[`lib/product-status.ts`](../apps/web/src/lib/product-status.ts) holds three
lookups. An import that completed with failed rows is `warn` with the count
in its label (`Completed — 2 rows failed`), the same "terminal is not
automatically good" rule as a ready document with no chunks. Availability
maps `IN_STOCK` to `success`, `PREORDER` to `info`, `OUT_OF_STOCK` to `warn`
(sellable again later, not broken) and `DISCONTINUED` to `neutral` (settled,
needs nothing). A product's search-index state gets a label only when it is
*not* indexed: the normal state earns no badge, so the exceptions are what a
scan of the table picks out — and the table explains them once, in a line
under it, rather than in a tooltip nobody hovers.

## Untrusted text, beyond citations

(A citation can name a product rather than a document chunk: `search_products`
and `get_product` cite the product row, with `document_title` carrying the
product's name. `ChatMessage`'s Sources list prefixes such an entry with a
muted "Product:" and keys every entry without assuming a chunk id exists —
a product citation has none, and a stored citation can lose its ids to a
deletion. The name is customer-supplied text under the same rule below.)

Phase 3 established the rule for citations: `document_title` and `excerpt`
are copied from an uploaded file with no server-side escaping, so rendering
is what has to hold the line — plain JSX text children only, never
`dangerouslySetInnerHTML` or a markdown pass. Phase 4 (Task 8) adds two more
sources under the same rule, because both are model output that can itself
be quoting a document or a visitor's own typed text:

- **A tool call's `arguments` and result**
  ([`components/chat/ToolCall.tsx`](../apps/web/src/components/chat/ToolCall.tsx)).
  A `create_lead` call's arguments *are* the visitor's own name/email/interest,
  typed back verbatim; a `retrieve_knowledge` result is an excerpt of a
  document's own content, exactly like a citation's excerpt. `summarizeArguments`
  stringifies each value into a one-line summary and the result sits behind a
  native `<details>`, but both are still JSX text children only.
- **A lead's own fields**
  ([`components/leads/LeadsTable.tsx`](../apps/web/src/components/leads/LeadsTable.tsx)).
  Same data, one hop further downstream: what `create_lead` captured is what
  the Leads page later lists.

- **A product's own fields**
  ([`components/products/ProductsTable.tsx`](../apps/web/src/components/products/ProductsTable.tsx)),
  added in Phase 5 (Task 6) — the fourth source, and the most direct: no
  model sits in between. A customer uploads a CSV and its `name`,
  `external_id`, `description`, `category` and `attributes` are rendered
  straight onto the page, as are an import's file name and the per-row
  error messages that can quote a row
  ([`ProductImports.tsx`](../apps/web/src/components/products/ProductImports.tsx)).
  `attributes` is heterogeneous `jsonb` with both keys and values
  customer-supplied, so the API flattens it to a list of `{key, value}`
  *string* pairs (a non-string value arrives as its JSON text) and the table
  renders each half as a text child of a `<dt>`/`<dd>` — the component never
  walks an arbitrary object, and a key is never used as anything but text.

The test shape this earns is the same one `ChatMessage.test.tsx` already
used for citations: render a `<script>` payload in the untrusted field and
assert `container.querySelector("script")` is `null` while
`screen.getByText(...)` still finds the literal string. A test that only
asserts the text is present would pass even if the component were rewritten
around `dangerouslySetInnerHTML` — the query selector is what actually pins
"never becomes markup".

## Uploads

[`components/knowledge/UploadDropzone.tsx`](../apps/web/src/components/knowledge/UploadDropzone.tsx)
is the one drag-and-drop-plus-picker in the app. The Products page reuses it
for catalogue imports by passing `acceptedTypes`, `maxBytes`, `pickLabel` and
its own `note`, rather than growing a second zone. The rules it holds for
every caller:

- **State the accepted types and the limit before a file is picked**, as help
  text on first render — never learned from a rejection.
- **The stated limit is floored** (`formatByteLimit`) so every file at or
  under the stated number actually passes. Imports and documents share one
  budget (`MAX_UPLOAD_BYTES`: the server's `max_request_bytes` less headroom
  for the multipart envelope), so there is one constant, not two.
- **`accept` lists extensions as well as mime types.** A `.csv` or `.md` on a
  machine with no registered association reports an empty type, and a
  mime-only filter can hide it from the picker.
- **The same checks run client-side first** (`validateDocumentFile`,
  `validateImportFile`) so the honest path never waits on a 422 or 413 — the
  server's checks remain the ones that count.

An upload whose outcome arrives later (a worker parses and embeds) shows that
outcome where the upload happened: the Products page lists recent imports
under the zone with their status, counts and — because "4 rows failed" is
not actionable — the failed rows themselves, by row number, SKU and reason.
The API returns the first 50 by row number (`errors(limit:)`, capped at 200),
and the list says "first 50 of N" when there are more.

An import can also complete with a warning about the whole batch — today only
that the rows landed but the embedding provider failed, so some are not yet
searchable by meaning. That is an `Alert tone="warn"` on the import, beside
its counts: a real, named outcome with a next step (import again), not a
failure — the `danger` alert stays for a whole-file failure.

The product list shows each row's **Last updated** time (`formatTimestamp`),
in its own column after Availability. A stale catalogue answers confidently
(`docs/PHASE-5.md` §8), and the date is how an owner spots one.

## Pagination

The Products list is the first that can outgrow a screen: a catalogue runs to
thousands of rows. It pages with Previous/Next over `limit`/`offset`, asking
for one row more than a page so whether a next page exists is known without
a count query. Any change to the search or filters returns to the first page.
Search is debounced (300 ms), so typing is not a request per letter.

## Tool calls in the transcript

A tool call is rendered as its own section under a message
(`ChatMessage`'s `ToolCalls`), not spliced into the answer text at the token
position it happened. The SSE stream (`tool_call_start`/`tool_call_end`,
`docs/PHASE-4.md` §4) reports *that* a call happened and its outcome, never
*where* within the surrounding text it fell — Phase 4 also removed the
Phase 3 guarantee that citations arrive before the first `text_delta`, since
retrieval is now a tool the model can call after already speaking. A section
that shows every call in the order it started is honest about what the wire
actually promises; guessing an inline position from delta counts would not
be.

`is_error` is the one signal a failed call has, and it must read as failed,
not as an empty success: `ToolCall` gives a failed call both a different
label (`Failed` vs. `Done`) and a different tone (`border-danger-line`
instead of `border-line`), because a state that only changes a text label
is too easy to miss scanning a transcript full of calls.

`step_limit_reached` arrives as an in-band `error` event (no `message_end`
for that turn) and gets its own `Alert tone="warn"` rather than the generic
`danger` treatment (`ChatMessage`'s `TurnError`) — see the `Alert` row above
and "Status as a tone" for why `warn`, specifically, is the tone for "a real,
named outcome, not a crash".

## A toggle without a new primitive

The agent Tools card
([`components/agents/AgentToolsCard.tsx`](../apps/web/src/components/agents/AgentToolsCard.tsx))
needed an enable/disable control and there is no dedicated switch in `ui/`.
Rather than add one, each row uses a plain `Button` whose label and variant
already encode the state (`Enable` / `primary` when off, `Disable` /
`secondary` when on) plus a `Badge` naming the current state next to it —
the same `loading` + `loadingLabel` pattern `DocumentsTable`'s Retry/Delete
buttons already use, scoped to one row at a time via a `togglingId` prop so
flipping one tool does not freeze the whole card. Reached for the existing
primitive per "Adding a component" below, rather than writing a second
control that would need its own focus ring and its own tests.

The card's own description says what the toggle *means*, not what the
defaults happen to be: "A disabled tool never runs, even if the model asks
for it." It used to read "Off until you turn it on here", which was wrong
twice over — `retrieve_knowledge` is on by default for every new agent, and
until the whole-branch review's Critical 1 fix a disabled tool ran anyway.
Copy that describes a guarantee ages better than copy that describes a
default, and this one is now true.

## Adding a component

1. Does a primitive already do it? Extend that one instead — a second thing
   that looks 90% like `Button` is how systems rot.
2. Colours come from tokens. If you need a value that has no token, add the
   token to `globals.css` and to `REQUIRED` in `globals.test.ts`.
3. Radius is `rounded-control` or `rounded-card`.
4. Focus uses `focusRing`, plus its own offset width.
5. Interactive controls start from `controlClasses`.
6. Put it in `ui/` only if it is domain-free. Otherwise it belongs beside its
   feature.
7. Accept `className` and merge it last with `cn`, so callers can size it
   without forking it.
8. Test what a caller would notice — the tone it renders, the aria wiring, the
   state it reports — not the class string.
