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
| `Alert` | a message about the page | `danger` gets `role="alert"` (interrupts a screen reader); `success` and `info` get `role="status"` (waits its turn). A save confirmation is not worth an interruption; a failure is. |
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
