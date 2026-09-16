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
| `Icon` | all iconography | One 24px stroked set in `icons.tsx`, `aria-hidden` by default because every icon here sits beside its own label. |

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
