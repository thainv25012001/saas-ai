# apps/web UI Foundation — Design

**Goal:** A business owner opens the app and can tell, without being told, what the
product is for, whether their assistant is ready, and what to do next. Today the UI
works but says nothing: it is unstyled create-next-app defaults wrapped around real
functionality.

**Scope:** `apps/web` only. **No API, GraphQL schema, or generated-type changes** —
every screen below renders from the two queries that already exist (`Agents`,
`Agent`). This is a presentation-layer change; streaming, auth, and tenancy logic are
untouched.

**Approach in one line:** Introduce the two layers the app never had — semantic design
tokens and a small primitives library — then rebuild the shell and all ten pages on
top of them.

---

## 1. Current state

Read of every file under `apps/web/src`. The gap is structural, not cosmetic.

**No tokens.** [`app/globals.css`](../../../apps/web/src/app/globals.css) is
unmodified create-next-app scaffolding:

- It declares `--background`/`--foreground` and a `@media (prefers-color-scheme: dark)`
  block that has **no effect** — `body` carries `bg-slate-50 text-slate-900` as utility
  classes (`app/layout.tsx:16`), and a class beats a bare `body` selector on
  specificity. Dead code that reads as a working dark mode.
- `@theme inline` maps `--font-sans` to `--font-geist-sans`, which is **never defined**
  (no `next/font` import anywhere). The declared `body` font is `Arial, Helvetica,
  sans-serif`, so the entire product renders in **Arial**.
- No colour, spacing, radius, or typographic scale exists. Pages name `slate-200`
  directly, 40+ times.

**No shared components.** Six of the ten pages are hand-assembled from repeated
literals:

| Duplicated thing | Occurrences | Drift already present |
|---|---|---|
| Input: `rounded-md border border-slate-300 px-3 py-2` | ~20 | `py-2` vs `py-1.5` |
| Primary button: `rounded-md bg-slate-900 px-4 py-2 text-sm text-white` | 9 | `rounded-md` vs `rounded-xl` cards |
| Danger alert: `rounded-md bg-red-50 p-3 text-sm text-red-700` | 7 | `mt-2` vs `mt-3` vs none |
| Card: `rounded-xl border border-slate-200 bg-white p-6` | 8 | `p-4` vs `p-6` |
| Bare "Loading…" string | 5 | `text-slate-500` only sometimes |
| `result.error?.graphQLErrors[0]?.message ?? null` | 4 | — |

Every visual fix currently costs 20 edits, and the drift shows that has stopped
happening.

**The shell does not fit the product.**
[`app/dashboard/layout.tsx`](../../../apps/web/src/app/dashboard/layout.tsx) is a
fixed `w-60` sidebar with:

- **No mobile behaviour.** Below ~640px the sidebar eats the viewport; nothing collapses.
- **No page-title region**, so each page re-invents an `<h1>` with its own spacing.
- **Four dead ends styled as live features.** Knowledge, Products, Leads, and Prompts
  are visually identical to Agents and Playground but land on
  [`ComingSoon`](../../../apps/web/src/components/ComingSoon.tsx). The intent (show the
  product's shape early) is sound; the execution wastes a click and erodes trust.
- **A broken active-route test.** `pathname === item.href` means viewing
  `/dashboard/agents/<id>` de-highlights **Agents** — the user is nowhere in the nav.
  A real defect, not a style issue.

**Overview earns nothing.** One card counting agents. For a product whose loop is
*configure → test → deploy*, it answers no question and offers one link.

**Playground is the product, and is cramped.** It carries streaming, per-message token
counts, cost, and latency — all of it real, all of it rendered as one grey run-on
sentence. It is boxed inside the shell's `p-8` content well with
`h-[calc(100vh-4rem)] max-h-[900px]`, a magic number that does not match the padding
it is compensating for, so the composer floats.

**Agent detail is a wall.** 300 lines, nine unlabelled fields in one flat stack.
`Temperature` sits beside `Max agent steps` with nothing saying the first is live and
the second is inert until Phase 4. `Provider` defaults to `fake` — the offline
provider — and the UI never says so, so a first-run user tests against a stub and
believes it is a model.

---

## 2. Approaches considered

The scope decision (foundation + all pages, current slate palette, no rebrand) is
settled. The open axis was where the primitives come from.

**A. Hand-rolled primitives on Tailwind v4 tokens — chosen.** ~10 small components,
zero new runtime dependencies, and inline SVG for the icons needed. Fits a repo that
today ships `next`, `react`, `urql`, `graphql` and nothing else — not even `clsx`.
The token layer means a future rebrand is one file.

**B. shadcn/ui + Radix + lucide-react.** Better long-term ergonomics for complex
widgets (dialogs, combobox, toasts) and accessibility handled for us. Rejected for
now: it adds ~6 runtime packages and a `components.json` build convention to serve an
app whose hardest widget is a `<select>`. Revisit when the first modal or command
palette is genuinely needed — the token names below are chosen to be compatible with
that migration.

**C. Styles only, no component layer.** Fix `globals.css`, add utility classes, leave
pages as literal-heavy JSX. Rejected: it fixes the palette but not the duplication or
the accessibility wiring, which is where the actual bugs are.

---

## 3. Design tokens

`app/globals.css` is rewritten. Every token is **semantic** — pages never name `slate`
again, which is what makes a later theme change a single-file edit.

```css
@import "tailwindcss";

@theme {
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;

  /* Surfaces, back to front */
  --color-canvas: oklch(98.4% 0.003 247.86);        /* app background  (slate-50)  */
  --color-surface: #ffffff;                          /* cards, sidebar              */
  --color-surface-muted: oklch(96.8% 0.007 247.90);  /* transcript, table head      */

  /* Lines */
  --color-line: oklch(92.9% 0.013 255.51);           /* card borders    (slate-200) */
  --color-line-strong: oklch(86.9% 0.022 252.89);    /* control borders (slate-300) */

  /* Ink */
  --color-ink: oklch(20.8% 0.042 265.75);            /* headings, body  (slate-900) */
  --color-ink-muted: oklch(44.6% 0.03 256.80);       /* secondary       (slate-600) */
  --color-ink-subtle: oklch(55.4% 0.046 257.42);     /* meta, disabled  (slate-500) */

  /* Primary action — deliberately the current slate-900, so no rebrand */
  --color-primary: oklch(20.8% 0.042 265.75);
  --color-primary-hover: oklch(27.9% 0.041 260.03);
  --color-primary-ink: #ffffff;

  /* Status: a surface + an ink per tone */
  --color-danger: oklch(44.4% 0.177 26.90);
  --color-danger-surface: oklch(96.1% 0.015 12.42);
  --color-danger-line: oklch(88.5% 0.062 18.33);
  --color-success: oklch(44.8% 0.119 151.33);
  --color-success-surface: oklch(96.2% 0.044 156.74);
  --color-info: oklch(48.8% 0.155 264.38);
  --color-info-surface: oklch(97% 0.014 254.60);

  --radius-control: 0.5rem;  /* inputs, buttons, badges */
  --radius-card: 0.75rem;    /* cards, panels           */
}
```

`body` keeps only `bg-canvas text-ink font-sans antialiased`. The dead dark-mode block
and the phantom Geist variables are **deleted** — light-only is the honest description
of what ships, and a real dark mode is out of scope (§10).

**Risk / first task:** confirm Tailwind v4's `@theme` generates utilities for custom
colour names (`bg-surface`, `text-ink-muted`, `border-line`) in this project's
`@tailwindcss/postcss ^4` setup. If it does not, fall back to `var(--color-*)` in the
primitives and keep the same token names — no page-level change either way.

---

## 4. Primitives — `components/ui/`

Small, single-purpose, each usable without reading its internals. The props listed are
the whole API; anything not listed is deliberately absent (§10).

| Component | API | Why it exists |
|---|---|---|
| `cn(...parts)` | joins truthy class strings | Conditional classes without adding `clsx`. |
| `Button` | `variant: "primary" \| "secondary" \| "danger"`, `size: "sm" \| "md"`, `loading?`, plus native button props | Replaces 9 copies. `loading` disables and swaps the label, retiring the hand-written `{fetching ? "Saving…" : "Save"}` pattern in 6 places. |
| `ButtonLink` | same variants, plus `next/link` props | The three places a link is styled as a button. |
| `Field` | `label`, `description?`, `error?`, `required?`, render-prop child | **The accessibility win.** Generates the id, wires `htmlFor`, `aria-describedby` (description + error), and `aria-invalid`. Today none of the 20 inputs has a description or an error association. |
| `Input` / `Textarea` / `Select` | native props, `forwardRef` | One border, one radius, one `focus-visible` ring — defined once. |
| `Card` + `CardHeader` + `CardBody` + `CardFooter` | `CardHeader: { title, description?, actions? }` | Replaces 8 ad-hoc card divs; the `description` slot is where field-group explanations go. |
| `PageHeader` | `title`, `description?`, `actions?`, `breadcrumb?` | Every page's `<h1>` and its spacing, decided once. |
| `Alert` | `tone: "danger" \| "success" \| "info"`, `title?` | Replaces 7 copies; picks `role="alert"` for danger and `role="status"` for success/info, which the current code gets right only by accident. |
| `Badge` | `tone: "neutral" \| "success" \| "warn" \| "info"` | Agent status, nav "Soon" markers, playground meta chips. |
| `EmptyState` | `icon?`, `title`, `description`, `action?` | The three empty states plus the four placeholder pages. |
| `Spinner` / `LoadingState` | `LoadingState: { label }` | Replaces the 5 bare "Loading…" strings. |
| `icons.tsx` | named 16/20px inline SVGs, `currentColor` | Fifteen glyphs: overview, agent, knowledge, product, lead, prompt, playground, chevron, plus, menu, close, check, warning, send, stop. No icon package. |

Deliberately **not** a primitive: the table. One page renders one table; a `DataTable`
abstraction now would be invented for imagined consumers. The agents table is styled
inline with tokens, and is promoted to a primitive when the second table appears.

**Shared helper:** `lib/graphql-errors.ts` — `firstGraphQLError(result): string | null`,
replacing the four copies of `result.error?.graphQLErrors[0]?.message ?? null`.

---

## 5. App shell

`app/dashboard/layout.tsx` drops from a 67-line monolith to a composition of three
focused files, because the responsive drawer and the nav config are both things you
want to read (and test) without the auth guard in the way.

```text
components/shell/nav.ts        — nav config + isActive(pathname, href)
components/shell/Sidebar.tsx   — brand, grouped nav, account footer
components/shell/TopBar.tsx    — mobile hamburger, current section, sign out
app/dashboard/layout.tsx       — auth guard + responsive frame only
```

**Nav config**, typed and grouped, so the shape of the product is legible at a glance:

```ts
type NavItem = {
  href: string;
  label: string;
  icon: IconName;
  state: "live" | "soon";
  phase?: string;            // shown on the placeholder page and as a tooltip
};
// Overview  (ungrouped)
// "Configure": Agents (live) · Prompts (soon, Phase 2) · Knowledge (soon, Phase 3)
//              · Products (soon, Phase 4)
// "Run":       Playground (live) · Leads (soon, Phase 4)
```

Grouping mirrors the product's actual loop — configure the assistant, then run it —
which is the cheapest way to make the sidebar explain the product. Items with
`state: "soon"` keep their link (the original intent: show the shape early) but carry a
`Soon` badge, so a click is an informed one rather than a dead end.

**`isActive(pathname, href)`** — exact match for `/dashboard`, prefix match on a path
segment boundary otherwise. This fixes the real defect where an agent's detail page
highlights nothing. Pure function, unit-tested (§9).

**Responsive frame.** The shell becomes a `h-dvh` flex column with
`<main className="flex-1 min-h-0 overflow-y-auto">`. That single change **deletes the
`h-[calc(100vh-4rem)] max-h-[900px]` magic number** from the playground: a child can
now just say `h-full`.

- `lg:` and up — sidebar always visible, 16rem.
- Below `lg:` — sidebar becomes an off-canvas drawer over a backdrop, opened from the
  `TopBar` hamburger, closed by backdrop click, `Escape`, or a route change.
- Content well — `mx-auto w-full max-w-6xl px-6 py-8`, with a `fullBleed` escape hatch
  the playground uses to fill the frame and scroll internally.

**Account footer** — org name, email, and `Sign out` move to the sidebar's bottom,
where an account block is expected, instead of sitting above the nav as they do now.

A skip link (`Skip to content` → `#main`) is added, and the loading branch renders
`LoadingState` rather than `<p className="p-8 text-slate-500">Loading…</p>`.

---

## 6. Pages

### Overview — `/dashboard`

Rebuilt to answer "is my assistant ready, and what do I do next?". Everything below
derives from the **existing** `Agents` query; nothing is faked and no API work is
needed.

1. **`PageHeader`** — "Overview", description naming the organization.
2. **Setup checklist** (the centrepiece), from a pure
   `lib/setup-checklist.ts` → `deriveChecklist(agents)`:
   - *Create an agent* — done when `agents.length > 0`.
   - *Connect a real model provider* — done when any agent has `provider !== "fake"`.
     This is the highest-value item on the page: `DEFAULT_LLM_PROVIDER` is `fake`, so
     **every** fresh install starts with an assistant that only pretends to answer, and
     nothing in today's UI says so.
   - *Activate an agent* — done when any agent is `ACTIVE`.
   - *Send a test message* — not derivable from the current schema, so it renders as an
     action ("Open playground"), never as a satisfied checkmark. Honest over tidy.
3. **Three stat tiles** — Total agents · Active · Draft. Counts, not invented metrics.
4. **Your agents** — first five, with status `Badge` and per-row *Configure* / *Test*
   links. Falls back to `EmptyState` with a "Create your first agent" action.
5. **What's next** — the remaining phases in one short list, replacing the vague
   "the backend catches up" prose.

### Agents — `/dashboard/agents`

The permanent create-form card above the table becomes a `New agent` action in the
`PageHeader` that discloses an inline form, so the page opens on the list — the thing
you came for. The table gets token styling, a `Badge` status, a mono slug, and a real
`EmptyState`. Create errors move into an `Alert` inside the disclosed form, next to the
field that caused them, rather than floating between the form and the table.

### Agent detail — `/dashboard/agents/[id]`

One flat stack of nine fields becomes four titled cards in a `max-w-3xl` column, each
`CardHeader.description` saying what the group controls:

- **Identity** — Name, Status.
- **Model** — Provider, Model, Temperature, Max tokens. Temperature and Max tokens sit
  in a 2-column grid (both are short numerics). Provider's `Field.description` states
  plainly that `fake` answers offline with a canned reply and costs nothing — the note
  the current UI's own source comment says users need. `Model` stays a free-text input
  because the API validates it per provider, but gains a `<datalist>` of known ids per
  provider so it is guessable instead of memorised.
- **Behaviour** — Tone, Retrieval top-K, Max agent steps, with the two inert fields
  labelled "takes effect in Phase 3 (retrieval)" / "Phase 4 (tools)" in their
  descriptions. Storing a value that does nothing yet is fine; not saying so is not.
- **Danger zone** — unchanged behaviour, `Alert`-based error, `Button variant="danger"`.

`PageHeader` carries a breadcrumb (Agents / name), the slug, a status `Badge`, and the
existing *Test in playground* link as a secondary action. The two-second "Saved"
banner keeps its behaviour but moves into `CardFooter` beside the submit button, so
saving no longer shifts the form under the user's cursor.

### Playground — `/dashboard/playground`

The product's core loop, and the page that gets the most care. **All streaming logic in
`lib/sse.ts` and `lib/chat-turn.ts`, and their tests, are untouched** — this is purely
how the turn is presented.

- **`fullBleed` three-row grid**: toolbar / transcript (the only scroll container) /
  composer pinned to the bottom. No viewport arithmetic.
- **Toolbar** — agent `Select`, a status `Badge` for the selected agent, a
  `New conversation` button, and a **session cost + token total** summed client-side
  from the `message_end` metadata already in state. The playground's job is to tell you
  what a real conversation costs; today that number exists per-message and is never
  totalled.
- **Transcript empty state** — instead of "Send a message to see the agent respond", an
  `EmptyState` with three clickable sales-flavoured example prompts that populate the
  composer. A playground that shows you what to ask explains the product in one screen.
- **`ChatMessage`** — user turns right-aligned on `primary`, assistant turns left on
  `surface` with a small role label. The streaming caret stays. The meta run-on line
  becomes four chips (model · tokens in/out · cost · latency), each with a `title`
  tooltip; `"not priced"` keeps its honest wording. The error and
  stopped-before-any-token states keep their current, already-correct handling and
  simply adopt `Alert`.
- **Composer** — a two-row `Textarea` the user can drag vertically to see more of a
  long prompt, a visible `Enter to send · Shift+Enter for a new line` hint, and `Stop`
  swapping in during a stream exactly as now.

### Auth — `/login`, `/register`

A shared `app/(auth)/layout.tsx` centres a `Card` under the product name and a
one-line statement of what the product does — this is the only place a new user meets
the product, and it is currently a bare form on a white page. Both forms move to
`Field`/`Input`/`Button`/`Alert`. Register's password field gains the description its
`minLength={12}` silently implies.

### Placeholder pages — Knowledge, Products, Leads, Prompts

`ComingSoon` is replaced by `PlaceholderPage({ title, phase, planned })`:
`PageHeader` + `EmptyState` + a short list of what will live there. Same honest
message, but it now tells you what you are waiting for.

---

## 7. Conventions (the point of the exercise)

After this change these hold everywhere, and a reviewer can cite them:

- **Loading** — `<LoadingState label="…" />`. Never a bare string.
- **Error** — `<Alert tone="danger">`, message via `firstGraphQLError(result)`.
- **Empty** — `<EmptyState>`.
- **Every form control** — inside a `Field`. No raw `<label>` wrapping an `<input>`.
- **Focus** — the `focus-visible` ring is defined in the primitives and nowhere else.
- **Colour** — pages use semantic tokens. A page naming `slate-*`, `red-*`, or
  `green-*` is a review failure.

---

## 8. File structure

```text
apps/web/src/
├── app/
│   ├── globals.css                    # rewritten: tokens (§3)
│   ├── layout.tsx                     # body → bg-canvas text-ink font-sans
│   ├── (auth)/layout.tsx              # NEW  shared auth frame
│   ├── (auth)/login/page.tsx          # → Field/Input/Button/Alert
│   ├── (auth)/register/page.tsx       # → Field/Input/Button/Alert
│   └── dashboard/
│       ├── layout.tsx                 # auth guard + responsive frame only
│       ├── page.tsx                   # rebuilt Overview (§6)
│       ├── agents/page.tsx            # disclosed create form + styled table
│       ├── agents/[id]/page.tsx       # four cards + field descriptions
│       ├── playground/page.tsx        # fullBleed grid + toolbar totals
│       └── {knowledge,products,leads,prompts}/page.tsx   # → PlaceholderPage
├── components/
│   ├── ui/                            # NEW  cn, Button, ButtonLink, Field, Input,
│   │                                  #      Textarea, Select, Card, PageHeader,
│   │                                  #      Alert, Badge, EmptyState, Spinner,
│   │                                  #      LoadingState, icons
│   ├── shell/                         # NEW  nav.ts, Sidebar, TopBar
│   ├── PlaceholderPage.tsx            # replaces ComingSoon.tsx (deleted)
│   └── chat/ChatMessage.tsx           # re-presented; same props
└── lib/
    ├── graphql-errors.ts              # NEW  firstGraphQLError
    ├── setup-checklist.ts             # NEW  deriveChecklist(agents)
    ├── chat-totals.ts                 # NEW  session token + cost sum
    ├── sse.ts  chat-turn.ts           # UNCHANGED
    └── auth.tsx  urql.tsx  api.ts     # UNCHANGED
```

---

## 9. Testing

The valuable logic is deliberately extracted into pure functions so it is testable
under the **existing** `vitest` setup with no new dependencies:

- `lib/setup-checklist.test.ts` — each step's done/not-done derivation, including the
  `provider === "fake"` case and the empty-agents case.
- `components/shell/nav.test.ts` — `isActive` exact-matches `/dashboard`, prefix-matches
  `/dashboard/agents/<id>` to Agents, and does **not** match `/dashboard/agentsomething`.
  This is the regression test for a defect that exists today.
- `lib/chat-totals.test.ts` — summing across turns with `costUsd: null` mixed in
  (must not silently become `0`), and the empty transcript.
- `lib/graphql-errors.test.ts` — first error, no error, network-error-only.

Component-level accessibility tests (`Field` wires `aria-describedby` and
`aria-invalid`; `Button loading` is disabled) need a DOM environment, which this repo
does not have. They are worth it for a layer every page depends on, so the plan adds
**dev-only** `@testing-library/react`, `@testing-library/jest-dom`, and `happy-dom`
plus `environment: "happy-dom"` in a new `vitest.config.ts`. No runtime dependency and
no production bundle impact.

Gates before each commit, as on the API side: `npm run typecheck`, `npm run lint`,
`npm run test`. The existing `sse.test.ts` and `chat-turn.test.ts` must stay green and
unmodified — they are the proof this change stayed in the presentation layer.

---

## 10. Out of scope (YAGNI)

Named explicitly so they do not creep in: dark mode (the dead CSS is deleted, not
implemented); `next/font` and a brand typeface; an icon or component library
(shadcn/Radix/lucide); toasts; a command palette; a generic `DataTable`; animation
beyond the existing streaming caret; charts; optimistic updates; real Knowledge,
Products, Leads, or Prompts functionality; any API, GraphQL, or codegen change.

---

## 11. Risks

1. **Tailwind v4 `@theme` custom colour names — resolved.** Verified against
   Tailwind 4.3.3 in this project: `bg-canvas`, `text-ink-muted`,
   `border-line`, and `rounded-card` all compile to `var(--color-*)` /
   `var(--radius-*)` references. `apps/web/src/app/globals.test.ts` asserts the
   full token contract on every run, so the fallback is not needed.
2. **A ten-page rewrite can regress behaviour.** Mitigated by touching no logic file:
   `sse.ts`, `chat-turn.ts`, `auth.tsx`, `urql.tsx`, and the generated GraphQL types
   are off-limits, and their tests must pass unmodified.
3. **The `oklch` values in §3 are transcriptions of the current Tailwind slate/red/green
   ramps.** They must be checked against rendered output rather than trusted, so that
   the "no rebrand" promise actually holds.
