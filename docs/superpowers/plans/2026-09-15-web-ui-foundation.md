# apps/web UI Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `apps/web` the two layers it never had — semantic design tokens and a small primitives library — then rebuild the dashboard shell and all ten pages on top of them, so the UI explains the product instead of merely exposing it.

**Architecture:** A one-file token layer in `globals.css` that every component reads through semantic names (`surface`, `ink`, `line`, `primary`, status tones), a hand-rolled `components/ui/` primitives library with no new runtime dependencies, and a `components/shell/` split that separates the nav data from the responsive frame from the auth guard. All logic worth testing is extracted into pure functions under `src/lib/`; pages become composition.

**Tech Stack:** Next.js 15.5.25 (App Router), React 19.1.0, TypeScript 5 (`strict`), Tailwind CSS v4.3.3 via `@tailwindcss/postcss`, urql 5, vitest 3.2.7.

**Spec:** [`docs/superpowers/specs/2026-09-15-web-ui-foundation-design.md`](../specs/2026-09-15-web-ui-foundation-design.md) — read it first; this plan argues from it.

## Global Constraints

- All commands run from **`apps/web`** unless stated otherwise.
- **Run `npm install` in `apps/web` before Task 1.** `vitest@^3.2.7` is in `package-lock.json` but absent from `node_modules`, so `npm run test` fails on a fresh clone until you do.
- **Baseline: 22 tests passing in 2 files** (`src/lib/sse.test.ts` 17, `src/lib/chat-turn.test.ts` 5). `npm run typecheck` is clean. Both must stay true at every commit.
- **No new runtime dependencies.** The only permitted `package.json` additions are dev-only, all in Task 1: `@testing-library/react`, `@testing-library/jest-dom`, `happy-dom`, `@vitejs/plugin-react`, `postcss`.
- **Do not modify these files.** They are logic, not presentation, and their behaviour is out of scope: `src/lib/sse.ts`, `src/lib/chat-turn.ts`, `src/lib/auth.tsx`, `src/lib/urql.tsx`, `src/lib/api.ts`, `src/middleware.ts`, `src/graphql/generated.ts`, `src/graphql/operations.graphql`, `codegen.ts`. **No API, GraphQL schema, or codegen change is part of this plan.**
- `src/lib/sse.test.ts` and `src/lib/chat-turn.test.ts` must pass **unmodified** — they are the proof this work stayed in the presentation layer.
- **Semantic tokens only.** No `slate-*`, `gray-*`, `red-*`, `green-*`, `blue-*`, `amber-*` (or any other raw Tailwind palette class) anywhere under `src/`. Task 16 adds a test that enforces this mechanically.
- **Gates before every commit:** `npm run typecheck`, `npm run lint`, `npm run test`. All three must pass. A commit with a failing gate is a defect.
- Every control **in a form** lives inside a `Field`. No raw `<label>` wrapping an `<input>`. The one exception, stated here so it is not mistaken for a slip: the playground's toolbar `Select` and composer `Textarea` are not form fields and use inline labels — `Field`'s stacked label-above-control block is wrong for a toolbar, and the composer's label is visually hidden by design.
- `lib/agent-status.ts` is one module the spec's §8 file list does not name. It exists because three pages render an agent's status and the `AgentStatus` → tone/label mapping is exactly the kind of derived value the spec says belongs in a tested pure function.
- Loading states use `<LoadingState label="…" />`; errors use `<Alert tone="danger">`; empties use `<EmptyState>`. Never a bare `"Loading…"` string.
- Conventional Commits, each message ending with exactly:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Resolved before planning (do not re-litigate):** Tailwind v4.3.3 in this project *does* generate utilities from custom `@theme` tokens — `bg-canvas`, `text-ink-muted`, `border-line`, and `rounded-card` were each compiled and verified against `@tailwindcss/postcss`. The spec's risk #1 is closed; build on tokens with confidence.
- **Two deliberate additions to the spec's §3 token list**, needed by primitives the spec itself specifies: `--color-warn` / `--color-warn-surface` / `--color-warn-line` (the `Badge tone="warn"` used for `DRAFT` and `Soon`), and `--color-success-line` / `--color-info-line` (so all four `Alert` tones have a border token rather than only `danger`). Task 2 includes them.
- **How pages are verified.** Page tasks (10–15) compose already-tested primitives and already-tested pure functions, so they carry no unit tests of their own; rendering them would require standing up urql and auth providers, which buys assertions about mocks rather than about the product. Their verification is `typecheck` + `lint` + the explicit visual checklist each task ends with, run against `npm run dev`. Say so honestly in the commit; do not claim test coverage a page task does not have.

---

## File Structure

```text
apps/web/
├── vitest.config.ts                    # NEW  T1: alias, react plugin, node default env
├── vitest.setup.ts                     # NEW  T1: jest-dom matchers + guarded RTL cleanup
└── src/
    ├── app/
    │   ├── globals.css                 # REWRITE T2: the token layer (only place colours are named)
    │   ├── layout.tsx                  # MODIFY  T2: body → bg-canvas text-ink font-sans
    │   ├── (auth)/layout.tsx           # NEW     T9: centred frame + product statement
    │   ├── (auth)/login/page.tsx       # REWRITE T9
    │   ├── (auth)/register/page.tsx    # REWRITE T9
    │   └── dashboard/
    │       ├── layout.tsx              # REWRITE T8: auth guard + responsive frame only
    │       ├── page.tsx                # REWRITE T10: Overview = setup checklist + tiles + agents
    │       ├── agents/page.tsx         # REWRITE T11: disclosed create form + tokenised table
    │       ├── agents/[id]/page.tsx    # REWRITE T12: four explained cards
    │       ├── playground/page.tsx     # REWRITE T14: three-row grid, toolbar totals
    │       └── {knowledge,products,
    │          leads,prompts}/page.tsx  # REWRITE T15: → PlaceholderPage
    ├── components/
    │   ├── ui/
    │   │   ├── cn.ts                   # NEW T3
    │   │   ├── Button.tsx              # NEW T3  Button, ButtonLink, buttonClasses
    │   │   ├── Field.tsx               # NEW T4  the a11y wiring
    │   │   ├── Input.tsx               # NEW T4  Input, Textarea, Select, controlClasses
    │   │   ├── Card.tsx                # NEW T5  Card, CardHeader, CardBody, CardFooter
    │   │   ├── PageHeader.tsx          # NEW T5
    │   │   ├── Alert.tsx               # NEW T5
    │   │   ├── Badge.tsx               # NEW T5  Badge, BadgeTone
    │   │   ├── EmptyState.tsx          # NEW T5
    │   │   ├── Spinner.tsx             # NEW T5  Spinner, LoadingState
    │   │   └── icons.tsx               # NEW T5  Icon, IconName (16 inline glyphs)
    │   ├── shell/
    │   │   ├── nav.ts                  # NEW T7  NAV_GROUPS, isActive, currentSectionLabel
    │   │   ├── Sidebar.tsx             # NEW T8
    │   │   └── TopBar.tsx              # NEW T8
    │   ├── PlaceholderPage.tsx         # NEW T15 (replaces ComingSoon.tsx, deleted)
    │   ├── ComingSoon.tsx              # DELETE T15
    │   └── chat/ChatMessage.tsx        # REWRITE T13 — same exported types, new presentation
    └── lib/
        ├── graphql-errors.ts           # NEW T6  firstGraphQLError
        ├── agent-status.ts             # NEW T6  agentStatusTone, agentStatusLabel
        ├── setup-checklist.ts          # NEW T6  deriveChecklist, checklistProgress
        └── chat-totals.ts              # NEW T6  sessionTotals
```

Why these boundaries: `nav.ts` holds data and one pure predicate so the sidebar's active-state bug becomes testable without rendering React; the four `lib/` modules exist so that every derived value on a page (which checklist step is met, what a session cost, how a status reads) is a pure function with a test, leaving the pages as pure composition; `ui/` files are split by widget because a reviewer judges `Field`'s accessibility wiring on entirely different grounds than `Button`'s variants.

---

## Task 1: Test infrastructure for component tests

**Files:**
- Create: `apps/web/vitest.config.ts`
- Create: `apps/web/vitest.setup.ts`
- Modify: `apps/web/package.json` (devDependencies only)
- Test: `apps/web/src/lib/test-setup.test.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces: a vitest setup where (a) `@/…` resolves to `src/…`, (b) `.tsx` files compile, (c) a test file opting in with the docblock `// @vitest-environment happy-dom` gets a DOM and `@testing-library/react`, and (d) every other test keeps the current `node` environment. Tasks 3–7 rely on all four.

- [ ] **Step 1: Install the dev-only dependencies**

```bash
cd apps/web
npm install --save-dev @testing-library/react@^16 @testing-library/jest-dom@^6 happy-dom@^20 @vitejs/plugin-react@^4 postcss@^8  # happy-dom@^20 (not ^15): the 15.x line has no fix for GHSA-37j7-fg3j-429f, a VM context escape / RCE advisory
```

`postcss` is already present transitively (`@tailwindcss/postcss` depends on it); this makes the dependency explicit because Task 2's test imports it directly. `@vitejs/plugin-react` is what gives vitest the JSX transform — without it, every `.tsx` test fails to parse.

- [ ] **Step 2: Write the failing test**

Create `apps/web/src/lib/test-setup.test.tsx`. This is a real test with a job: it proves the three things the next five tasks assume (DOM env, JSX transform, `@/` alias, jest-dom matchers) rather than leaving them to be discovered one broken task at a time.

```tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { cn } from "@/components/ui/cn";

describe("component test environment", () => {
  it("renders JSX into a DOM and matches with jest-dom", () => {
    render(<p data-testid="probe">hello</p>);
    expect(screen.getByTestId("probe")).toBeInTheDocument();
  });

  it("resolves the @/ alias to src/", () => {
    expect(cn("a", false, "b", undefined)).toBe("a b");
  });
});
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `npm run test -- src/lib/test-setup.test.tsx`
Expected: FAIL — `Cannot find module '@/components/ui/cn'` (no alias, and `cn.ts` does not exist yet).

- [ ] **Step 4: Create the vitest config**

`apps/web/vitest.config.ts`:

```ts
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    // `node` stays the default so the existing sse/chat-turn suites run under
    // exactly the conditions they were written and verified against. Component
    // tests opt in per file with `// @vitest-environment happy-dom`.
    environment: "node",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
```

- [ ] **Step 5: Create the setup file**

`apps/web/vitest.setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// This setup file runs for every test file, including the `node`-environment
// ones, where there is no document to clean up and `cleanup()` would throw.
afterEach(() => {
  if (typeof document !== "undefined") cleanup();
});
```

- [ ] **Step 6: Create the module the alias test imports**

`apps/web/src/components/ui/cn.ts`:

```ts
/** A class value that may be conditionally absent. */
export type ClassValue = string | false | null | undefined;

/**
 * Joins the truthy class names. This is the whole of what `clsx` would give
 * us here, and the repo carries no UI dependencies (see the spec, §2).
 */
export function cn(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(" ");
}
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `npm run test -- src/lib/test-setup.test.tsx`
Expected: PASS, 2 tests.

- [ ] **Step 8: Run the full suite and the other gates**

Run: `npm run test`
Expected: PASS — **24 tests in 3 files** (the 22 baseline plus these 2).

Run: `npm run typecheck` then `npm run lint`
Expected: both clean.

- [ ] **Step 9: Commit**

```bash
git add apps/web/package.json apps/web/package-lock.json apps/web/vitest.config.ts apps/web/vitest.setup.ts apps/web/src/components/ui/cn.ts apps/web/src/lib/test-setup.test.tsx
git commit -m "$(cat <<'MSG'
test(web): add a DOM environment for component tests

vitest gets an @/ alias, the React JSX transform, and happy-dom available
per-file via a docblock. `node` stays the default environment so the
existing sse and chat-turn suites run under unchanged conditions.

All five additions are devDependencies; no runtime dependency is added.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 2: The design token layer

**Files:**
- Rewrite: `apps/web/src/app/globals.css` (all 26 lines)
- Modify: `apps/web/src/app/layout.tsx:16` (the `<body>` className)
- Test: `apps/web/src/app/globals.test.ts`

**Interfaces:**
- Consumes: `postcss` (Task 1, step 1).
- Produces: the utility classes every later task uses. Exact names, and nothing outside this list is a token:
  `bg-canvas` `bg-surface` `bg-surface-muted` · `border-line` `border-line-strong` · `text-ink` `text-ink-muted` `text-ink-subtle` · `bg-primary` `hover:bg-primary-hover` `text-primary-ink` · per status tone `{danger,success,warn,info}`: `text-<tone>`, `bg-<tone>-surface`, `border-<tone>-line` · `rounded-card` `rounded-control` · `font-sans` `font-mono`.

- [ ] **Step 1: Write the failing test**

`apps/web/src/app/globals.test.ts` — compiles the real stylesheet and asserts the tokens produce utilities. This is the test that protects the entire strategy: if a token is renamed or dropped, every page that used it silently loses its colour, and only this test notices.

```ts
import { readFileSync } from "node:fs";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";
import { describe, expect, it } from "vitest";

/** Compiles `globals.css` plus a probe rule per utility, and returns the
 * generated declarations for each probe. `source(none)` keeps Tailwind from
 * scanning the project, so the output depends only on the token layer. */
async function compileProbes(utilities: readonly string[]): Promise<Record<string, string>> {
  const tokens = readFileSync("src/app/globals.css", "utf8").replace(
    '@import "tailwindcss";',
    '@import "tailwindcss" source(none);',
  );
  const probes = utilities
    .map((utility, index) => `.probe-${index} { @apply ${utility}; }`)
    .join("\n");
  const { css } = await postcss([tailwind]).process(`${tokens}\n${probes}`, {
    from: "src/app/globals.css",
  });
  return Object.fromEntries(
    utilities.map((utility, index) => {
      const match = css.match(new RegExp(`\\.probe-${index}\\s*\\{([^}]*)\\}`, "s"));
      return [utility, (match?.[1] ?? "").replace(/\s+/g, " ").trim()];
    }),
  );
}

const REQUIRED = [
  "bg-canvas",
  "bg-surface",
  "bg-surface-muted",
  "border-line",
  "border-line-strong",
  "text-ink",
  "text-ink-muted",
  "text-ink-subtle",
  "bg-primary",
  "text-primary-ink",
  "text-danger",
  "bg-danger-surface",
  "border-danger-line",
  "text-success",
  "bg-success-surface",
  "border-success-line",
  "text-warn",
  "bg-warn-surface",
  "border-warn-line",
  "text-info",
  "bg-info-surface",
  "border-info-line",
  "rounded-card",
  "rounded-control",
] as const;

describe("globals.css token layer", () => {
  it("generates a utility for every semantic token pages rely on", async () => {
    const compiled = await compileProbes(REQUIRED);
    const missing = REQUIRED.filter((utility) => compiled[utility] === "");
    expect(missing).toEqual([]);
  });

  it("maps each utility to its token variable rather than a literal colour", async () => {
    const compiled = await compileProbes(["bg-surface-muted", "text-ink-muted", "rounded-card"]);
    expect(compiled["bg-surface-muted"]).toContain("var(--color-surface-muted)");
    expect(compiled["text-ink-muted"]).toContain("var(--color-ink-muted)");
    expect(compiled["rounded-card"]).toContain("var(--radius-card)");
  });

  it("does not reinstate the create-next-app leftovers", () => {
    const css = readFileSync("src/app/globals.css", "utf8");
    // A font variable that was never defined (so the app rendered in Arial)
    // and a dark-mode block that the body's utility classes overrode anyway.
    expect(css).not.toContain("geist");
    expect(css).not.toContain("prefers-color-scheme");
  });

  it("matches the Tailwind palette it claims to transcribe — this is not a rebrand", async () => {
    // The claim "same colours, new structure" is worth only as much as its
    // evidence, and hand-transcribed oklch triples are exactly the kind of
    // thing that drifts silently. So take ground truth from Tailwind itself:
    // compile a utility for BOTH the token and its palette equivalent, then
    // compare the two variables Tailwind emits.
    //
    // Both sides must be referenced by a utility. Tailwind tree-shakes theme
    // variables, so a token no rule uses is simply absent from the output and
    // would compare as null == null.
    const EQUIVALENTS: Record<string, string> = {
      canvas: "slate-50",
      "surface-muted": "slate-100",
      line: "slate-200",
      "line-strong": "slate-300",
      "ink-subtle": "slate-500",
      "ink-muted": "slate-600",
      ink: "slate-900",
      primary: "slate-900",
      "primary-hover": "slate-800",
      danger: "red-700",
      "danger-surface": "red-50",
      "danger-line": "red-200",
      success: "green-700",
      "success-surface": "green-50",
      "success-line": "green-200",
      warn: "amber-700",
      "warn-surface": "amber-50",
      "warn-line": "amber-200",
      info: "blue-700",
      "info-surface": "blue-50",
      "info-line": "blue-200",
    };

    const tokens = readFileSync("src/app/globals.css", "utf8").replace(
      '@import "tailwindcss";',
      '@import "tailwindcss" source(none);',
    );
    const uses = Object.entries(EQUIVALENTS)
      .flatMap(([token, palette], index) => [
        `.tok-${index} { @apply bg-${token}; }`,
        `.pal-${index} { @apply bg-${palette}; }`,
      ])
      .join("\n");
    const { css } = await postcss([tailwind]).process(`${tokens}\n${uses}`, {
      from: "src/app/globals.css",
    });

    const valueOf = (variable: string): string | null => {
      const match = css.match(new RegExp(`--color-${variable}:\\s*([^;]+);`));
      return match ? match[1].trim() : null;
    };

    const mismatches = Object.entries(EQUIVALENTS)
      .filter(([token, palette]) => valueOf(token) !== valueOf(palette))
      .map(([token, palette]) => `${token}=${valueOf(token)} but ${palette}=${valueOf(palette)}`);
    expect(mismatches).toEqual([]);
  });
});
```

`surface` and `primary-ink` are absent from `EQUIVALENTS` on purpose: both are `#ffffff`, which is not a palette entry.

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/app/globals.test.ts`
Expected: FAIL on the first two tests (no tokens exist yet) and on the third (`prefers-color-scheme` is still in the file).

- [ ] **Step 3: Rewrite `globals.css`**

Replace the entire contents of `apps/web/src/app/globals.css`:

```css
@import "tailwindcss";

/* The only place in `src/` where a colour is named. Every component reads
 * these through semantic utilities (`bg-surface`, `text-ink-muted`), so a
 * future theme change happens here and nowhere else.
 *
 * The values are the Tailwind slate/red/green/amber/blue ramps the UI already
 * used, transcribed deliberately: this change restructures the UI without
 * rebranding it. */
@theme {
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;

  /* Surfaces, back to front */
  --color-canvas: oklch(98.4% 0.003 247.858); /* app background    = slate-50  */
  --color-surface: #ffffff; /*                   cards, sidebar               */
  --color-surface-muted: oklch(96.8% 0.007 247.896); /* transcript = slate-100 */

  /* Lines */
  --color-line: oklch(92.9% 0.013 255.508); /*      card borders   = slate-200 */
  --color-line-strong: oklch(86.9% 0.022 252.894); /* controls     = slate-300 */

  /* Ink */
  --color-ink: oklch(20.8% 0.042 265.755); /*       headings, body = slate-900 */
  --color-ink-muted: oklch(44.6% 0.043 257.281); /* secondary      = slate-600 */
  --color-ink-subtle: oklch(55.4% 0.046 257.417); /* meta          = slate-500 */

  /* Primary action -- deliberately the slate-900 the UI already used */
  --color-primary: oklch(20.8% 0.042 265.755); /*                  = slate-900 */
  --color-primary-hover: oklch(27.9% 0.041 260.031); /*            = slate-800 */
  --color-primary-ink: #ffffff;

  /* Status: an ink, a surface, and a line per tone. danger and success are
   * red-700/red-50 and green-700/green-50 -- exactly what the old alert and
   * "Saved" blocks used, so those two tones are unchanged on screen. */
  --color-danger: oklch(50.5% 0.213 27.518); /*                      = red-700 */
  --color-danger-surface: oklch(97.1% 0.013 17.38); /*               = red-50  */
  --color-danger-line: oklch(88.5% 0.062 18.334); /*                 = red-200 */

  --color-success: oklch(52.7% 0.154 150.069); /*                   = green-700 */
  --color-success-surface: oklch(98.2% 0.018 155.826); /*           = green-50  */
  --color-success-line: oklch(92.5% 0.084 155.995); /*              = green-200 */

  --color-warn: oklch(55.5% 0.163 48.998); /*                       = amber-700 */
  --color-warn-surface: oklch(98.7% 0.022 95.277); /*               = amber-50  */
  --color-warn-line: oklch(92.4% 0.12 95.746); /*                   = amber-200 */

  --color-info: oklch(48.8% 0.243 264.376); /*                       = blue-700 */
  --color-info-surface: oklch(97% 0.014 254.604); /*                 = blue-50  */
  --color-info-line: oklch(88.2% 0.059 254.128); /*                  = blue-200 */

  --radius-control: 0.5rem; /* inputs, buttons, badges */
  --radius-card: 0.75rem; /*   cards, panels          */
}
```

Everything else the old file contained is gone on purpose: `--background`/`--foreground` (superseded), the `prefers-color-scheme: dark` block (it never took effect — the body's utility classes outrank a bare `body` selector — and real dark mode is out of scope), the `@theme inline` block referencing the undefined `--font-geist-sans`, and the `font-family: Arial` declaration that was therefore what actually rendered.

- [ ] **Step 4: Point the body at the tokens**

In `apps/web/src/app/layout.tsx`, replace the `<body>` line:

```tsx
      <body className="min-h-screen bg-canvas font-sans text-ink antialiased">
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm run test -- src/app/globals.test.ts`
Expected: PASS, 4 tests.

- [ ] **Step 6: Confirm the app renders**

The spec's risk 3 — that a hand-transcribed `oklch` triple can be wrong in a way that still compiles — is now covered by the palette-equality test in step 1 rather than by eyeballing a screenshot. That test is the gate; this step only confirms the page still loads.

Run `npm run dev` and open `http://localhost:3000/login`.
Expected: the form renders, the background and borders look unremarkable, and the typeface is now the system UI font (Segoe UI on Windows) instead of Arial — the one intended visible change. Stop the dev server.

**The token values in step 3 are ground truth, already verified against Tailwind 4.3.3 in this project — do not "correct" them.** Nine of them were wrong in an earlier draft of this plan, which is why the test exists.

- [ ] **Step 7: Run the gates**

Run: `npm run test` (expected: **28 tests in 4 files**), then `npm run typecheck`, then `npm run lint`.
Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/app/globals.css apps/web/src/app/layout.tsx apps/web/src/app/globals.test.ts
git commit -m "$(cat <<'MSG'
feat(web): replace the create-next-app CSS with semantic design tokens

globals.css becomes the one place a colour is named: surfaces, lines, ink,
a primary action, and four status tones, each reachable as a Tailwind
utility. Pages stop naming slate-*.

Three leftovers are deleted rather than carried forward: a
prefers-color-scheme block the body's utility classes always overrode, an
@theme mapping --font-sans to an undefined --font-geist-sans, and the
`font-family: Arial` that was consequently the app's real typeface.

The token contract is compiled and asserted in globals.test.ts, so renaming
or dropping a token fails a test instead of silently losing a colour.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 3: `Button` and `ButtonLink`

**Files:**
- Create: `apps/web/src/components/ui/Button.tsx`
- Test: `apps/web/src/components/ui/Button.test.tsx`

**Interfaces:**
- Consumes: `cn` from `@/components/ui/cn` (Task 1), tokens from Task 2.
- Produces:
  - `type ButtonVariant = "primary" | "secondary" | "danger"`
  - `type ButtonSize = "sm" | "md"`
  - `buttonClasses(options?: { variant?: ButtonVariant; size?: ButtonSize; className?: string }): string`
  - `Button` — `React.ButtonHTMLAttributes<HTMLButtonElement>` plus `{ variant?: ButtonVariant; size?: ButtonSize; loading?: boolean; loadingLabel?: string }`, `forwardRef` to `HTMLButtonElement`
  - `ButtonLink` — `next/link`'s props plus `{ variant?: ButtonVariant; size?: ButtonSize }`

- [ ] **Step 1: Write the failing test**

`apps/web/src/components/ui/Button.test.tsx`:

```tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "./Button";

describe("Button", () => {
  it("shows the loading label and disables itself while loading", () => {
    render(
      <Button loading loadingLabel="Saving…">
        Save agent
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Saving…");
    expect(button).not.toHaveTextContent("Save agent");
  });

  it("shows its children and stays enabled when not loading", () => {
    render(<Button loadingLabel="Saving…">Save agent</Button>);
    const button = screen.getByRole("button");
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Save agent");
  });

  it("stays disabled when disabled is passed without loading", () => {
    render(<Button disabled>Send</Button>);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("keeps the caller's own classes alongside the variant classes", () => {
    render(
      <Button variant="danger" className="mt-3">
        Delete
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button.className).toContain("mt-3");
    expect(button.className).toContain("text-danger");
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/components/ui/Button.test.tsx`
Expected: FAIL — `Failed to resolve import "./Button"`.

- [ ] **Step 3: Write the implementation**

`apps/web/src/components/ui/Button.tsx`:

```tsx
import Link from "next/link";
import { forwardRef } from "react";
import { cn } from "./cn";

export type ButtonVariant = "primary" | "secondary" | "danger";
export type ButtonSize = "sm" | "md";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-control font-medium transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink focus-visible:ring-offset-2 " +
  "focus-visible:ring-offset-canvas disabled:cursor-not-allowed disabled:opacity-50";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-primary text-primary-ink hover:bg-primary-hover",
  secondary: "border border-line-strong bg-surface text-ink hover:bg-surface-muted",
  danger: "border border-danger-line bg-surface text-danger hover:bg-danger-surface",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "px-3 py-1.5 text-sm",
  md: "px-4 py-2 text-sm",
};

/** Shared so `ButtonLink` and `Button` cannot drift apart. */
export function buttonClasses({
  variant = "primary",
  size = "md",
  className,
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
} = {}): string {
  return cn(BASE, VARIANTS[variant], SIZES[size], className);
}

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Disables the button and, with `loadingLabel`, swaps the label. */
  loading?: boolean;
  loadingLabel?: string;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant, size, loading = false, loadingLabel, className, disabled, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      // Every call site before this component wrote `disabled={fetching}` and
      // `{fetching ? "Saving…" : "Save"}` by hand; both now follow from one prop.
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses({ variant, size, className })}
      {...rest}
    >
      {loading && loadingLabel ? loadingLabel : children}
    </button>
  );
});

export type ButtonLinkProps = React.ComponentProps<typeof Link> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
};

export function ButtonLink({ variant, size, className, ...rest }: ButtonLinkProps) {
  return <Link className={buttonClasses({ variant, size, className })} {...rest} />;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm run test -- src/components/ui/Button.test.tsx`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the gates and commit**

Run: `npm run test` (expected: **32 tests in 5 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/components/ui/Button.tsx apps/web/src/components/ui/Button.test.tsx
git commit -m "$(cat <<'MSG'
feat(web): add the Button primitive

Three variants and two sizes replace nine hand-copied class strings, and
`loading` + `loadingLabel` replace the six hand-written
`{fetching ? "Saving…" : "Save"}` ternaries. ButtonLink shares the same
class function so a link styled as a button cannot drift from a real one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 4: `Field`, `Input`, `Textarea`, `Select`

**Files:**
- Create: `apps/web/src/components/ui/Field.tsx`
- Create: `apps/web/src/components/ui/Input.tsx`
- Test: `apps/web/src/components/ui/Field.test.tsx`

**Interfaces:**
- Consumes: `cn` (Task 1), tokens (Task 2).
- Produces:
  - `type FieldControlProps = { id: string; "aria-describedby": string | undefined; "aria-invalid": true | undefined; required: boolean | undefined }`
  - `Field` — `{ label: string; description?: string; error?: string | null; required?: boolean; children: (control: FieldControlProps) => React.ReactNode }`
  - `controlClasses: string`
  - `Input`, `Textarea`, `Select` — native props, each `forwardRef`

Every form on every page consumes `Field`'s render prop by spreading it onto the control: `<Field label="Name" required>{(control) => <Input {...control} value={…} onChange={…} />}</Field>`.

- [ ] **Step 1: Write the failing test**

`apps/web/src/components/ui/Field.test.tsx`. These four assertions are the accessibility floor the current UI does not meet anywhere — none of its 20 inputs has a description or an error associated with it.

```tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Field } from "./Field";
import { Input } from "./Input";

describe("Field", () => {
  it("associates the label with the control", () => {
    render(<Field label="Agent name">{(control) => <Input {...control} />}</Field>);
    // getByLabelText only finds it if htmlFor and id actually match.
    expect(screen.getByLabelText("Agent name")).toBeInTheDocument();
  });

  it("describes the control with its description text", () => {
    render(
      <Field label="Provider" description="`fake` answers offline and costs nothing.">
        {(control) => <Input {...control} />}
      </Field>,
    );
    expect(screen.getByLabelText("Provider")).toHaveAccessibleDescription(
      "`fake` answers offline and costs nothing.",
    );
  });

  it("marks the control invalid and describes it with the error", () => {
    render(
      <Field label="Model" description="Must exist for the provider." error="Unknown model.">
        {(control) => <Input {...control} />}
      </Field>,
    );
    const input = screen.getByLabelText("Model");
    expect(input).toHaveAttribute("aria-invalid", "true");
    // Both the description and the error are announced, in that order.
    expect(input).toHaveAccessibleDescription("Must exist for the provider. Unknown model.");
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown model.");
  });

  it("sets no aria-describedby when there is nothing to describe", () => {
    render(<Field label="Tone">{(control) => <Input {...control} />}</Field>);
    expect(screen.getByLabelText("Tone")).not.toHaveAttribute("aria-describedby");
  });

  it("passes required through to the control", () => {
    render(<Field label="Email" required>{(control) => <Input {...control} />}</Field>);
    expect(screen.getByLabelText(/Email/)).toBeRequired();
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/components/ui/Field.test.tsx`
Expected: FAIL — `Failed to resolve import "./Field"`.

- [ ] **Step 3: Write `Field`**

`apps/web/src/components/ui/Field.tsx`:

```tsx
"use client";

import { useId } from "react";

/** What a `Field` hands its control. Spread it: `<Input {...control} />`. */
export type FieldControlProps = {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
  required: boolean | undefined;
};

export type FieldProps = {
  label: string;
  /** Says what the field does, or what a value means. Announced with the label. */
  description?: string;
  error?: string | null;
  required?: boolean;
  children: (control: FieldControlProps) => React.ReactNode;
};

/**
 * Owns the wiring that is easy to forget and invisible when missing: a
 * generated id shared by the label and the control, `aria-describedby`
 * pointing at the description and the error, and `aria-invalid`.
 */
export function Field({ label, description, error, required, children }: FieldProps) {
  const id = useId();
  const descriptionId = description ? `${id}-description` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [descriptionId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium text-ink">
        {label}
        {required ? (
          <span aria-hidden className="ml-0.5 text-danger">
            *
          </span>
        ) : null}
      </label>

      {/* Before the control, so it is read after the label and before the value. */}
      {description ? (
        <p id={descriptionId} className="text-xs text-ink-subtle">
          {description}
        </p>
      ) : null}

      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
        required,
      })}

      {error ? (
        <p id={errorId} role="alert" className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 4: Write the controls**

`apps/web/src/components/ui/Input.tsx`:

```tsx
import { forwardRef } from "react";
import { cn } from "./cn";

/** One border, one radius, one focus ring — for all three controls. */
export const controlClasses =
  "w-full rounded-control border border-line-strong bg-surface px-3 py-2 text-sm text-ink " +
  "placeholder:text-ink-subtle focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ink focus-visible:ring-offset-1 focus-visible:ring-offset-canvas " +
  "disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger";

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(controlClasses, className)} {...rest} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={cn(controlClasses, "resize-none", className)} {...rest} />;
});

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...rest }, ref) {
    return <select ref={ref} className={cn(controlClasses, "pr-8", className)} {...rest} />;
  },
);
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm run test -- src/components/ui/Field.test.tsx`
Expected: PASS, 5 tests.

- [ ] **Step 6: Run the gates and commit**

Run: `npm run test` (expected: **37 tests in 6 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/components/ui/Field.tsx apps/web/src/components/ui/Input.tsx apps/web/src/components/ui/Field.test.tsx
git commit -m "$(cat <<'MSG'
feat(web): add Field plus the Input, Textarea and Select controls

Field owns the wiring that is invisible when missing: a generated id shared
by label and control, aria-describedby pointing at both the description and
the error, and aria-invalid. None of the twenty inputs in the app had any of
it, and no field could explain itself.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 5: The remaining display primitives

**Files:**
- Create: `apps/web/src/components/ui/Card.tsx`
- Create: `apps/web/src/components/ui/PageHeader.tsx`
- Create: `apps/web/src/components/ui/Alert.tsx`
- Create: `apps/web/src/components/ui/Badge.tsx`
- Create: `apps/web/src/components/ui/EmptyState.tsx`
- Create: `apps/web/src/components/ui/Spinner.tsx`
- Create: `apps/web/src/components/ui/icons.tsx`
- Test: `apps/web/src/components/ui/Alert.test.tsx`

**Interfaces:**
- Consumes: `cn` (Task 1), `buttonClasses` is *not* used here, tokens (Task 2).
- Produces:
  - `Card`, `CardHeader({ title, description?, actions? })`, `CardBody`, `CardFooter` — each also accepts `className` and `children`
  - `PageHeader({ title, description?, meta?, actions?, breadcrumb? })` where `breadcrumb?: { href: string; label: string }[]`
  - `type AlertTone = "danger" | "success" | "info"`; `Alert({ tone, title?, children, className? })`
  - `type BadgeTone = "neutral" | "success" | "warn" | "info"`; `Badge({ tone?, children, className?, title? })`
  - `EmptyState({ icon?, title, description, action? })` where `icon?: IconName`
  - `Spinner({ className? })`, `LoadingState({ label })`
  - `type IconName`, `Icon({ name, className? })` — 16 glyphs: `overview` `agent` `knowledge` `product` `lead` `prompt` `playground` `chevronRight` `plus` `menu` `close` `check` `circle` `warning` `send` `stop`

The spec's §4 named fifteen glyphs; `circle` is added because the Overview checklist needs a marker for a step that is *not* yet done, and an empty box is that marker.

- [ ] **Step 1: Write the failing test**

`apps/web/src/components/ui/Alert.test.tsx`. `Alert` is the only one of these seven with behaviour worth asserting — the role it announces itself with. The current code picks `role="alert"` for errors and `role="status"` for saves correctly only by coincidence, and a success banner that announces itself as an alert interrupts a screen reader for good news.

```tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Alert } from "./Alert";

describe("Alert", () => {
  it("announces a danger alert assertively", () => {
    render(<Alert tone="danger">Unknown model.</Alert>);
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown model.");
  });

  it("announces success and info politely, as a status", () => {
    render(<Alert tone="success">Saved</Alert>);
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders its title above the message", () => {
    render(
      <Alert tone="danger" title="Could not save">
        The model is not available for this provider.
      </Alert>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not save");
    expect(alert).toHaveTextContent("The model is not available for this provider.");
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/components/ui/Alert.test.tsx`
Expected: FAIL — `Failed to resolve import "./Alert"`.

- [ ] **Step 3: Write `icons.tsx`**

`apps/web/src/components/ui/icons.tsx`. Simple geometry drawn with `currentColor` so a glyph inherits whatever colour its context sets — no icon package, per the spec's §2.

```tsx
import { cn } from "./cn";

export type IconName =
  | "overview"
  | "agent"
  | "knowledge"
  | "product"
  | "lead"
  | "prompt"
  | "playground"
  | "chevronRight"
  | "plus"
  | "menu"
  | "close"
  | "check"
  | "circle"
  | "warning"
  | "send"
  | "stop";

const GLYPHS: Record<IconName, React.ReactNode> = {
  overview: (
    <>
      <rect x="3" y="3" width="8" height="8" rx="2" />
      <rect x="13" y="3" width="8" height="8" rx="2" />
      <rect x="3" y="13" width="8" height="8" rx="2" />
      <rect x="13" y="13" width="8" height="8" rx="2" />
    </>
  ),
  agent: (
    <>
      <path d="M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4z" />
      <circle cx="9.5" cy="9.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="14.5" cy="9.5" r="1" fill="currentColor" stroke="none" />
    </>
  ),
  knowledge: (
    <>
      <path d="M6 3h8l5 5v13H6z" />
      <path d="M14 3v5h5" />
      <path d="M9 13h6" />
      <path d="M9 17h6" />
    </>
  ),
  product: (
    <>
      <path d="M12 3l9 4.5v9L12 21l-9-4.5v-9z" />
      <path d="M3 7.5L12 12l9-4.5" />
      <path d="M12 12v9" />
    </>
  ),
  lead: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>
  ),
  prompt: (
    <>
      <path d="M9 4H7.5A2.5 2.5 0 0 0 5 6.5v3A2.5 2.5 0 0 1 2.5 12A2.5 2.5 0 0 1 5 14.5v3A2.5 2.5 0 0 0 7.5 20H9" />
      <path d="M15 4h1.5A2.5 2.5 0 0 1 19 6.5v3A2.5 2.5 0 0 0 21.5 12A2.5 2.5 0 0 0 19 14.5v3A2.5 2.5 0 0 1 16.5 20H15" />
    </>
  ),
  playground: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="M10 8.5L16 12l-6 3.5z" />
    </>
  ),
  chevronRight: <path d="M9 6l6 6-6 6" />,
  plus: (
    <>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </>
  ),
  menu: (
    <>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </>
  ),
  close: (
    <>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </>
  ),
  check: <path d="M5 13l4 4 10-10" />,
  circle: <circle cx="12" cy="12" r="8" />,
  warning: (
    <>
      <path d="M12 4l9 15H3z" />
      <path d="M12 10v4" />
      <circle cx="12" cy="16.75" r="0.75" fill="currentColor" stroke="none" />
    </>
  ),
  send: (
    <>
      <path d="M21 4L3 11l7 2.5L12.5 21z" />
      <path d="M21 4l-11 9.5" />
    </>
  ),
  stop: <rect x="7" y="7" width="10" height="10" rx="2" />,
};

/** Decorative by default: every icon in this UI sits beside its own label. */
export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("size-5 shrink-0", className)}
    >
      {GLYPHS[name]}
    </svg>
  );
}
```

- [ ] **Step 4: Write `Alert.tsx`**

```tsx
import { cn } from "./cn";
import { Icon } from "./icons";

export type AlertTone = "danger" | "success" | "info";

const TONES: Record<AlertTone, { classes: string; role: "alert" | "status" }> = {
  // `alert` interrupts a screen reader; `status` waits its turn. A save
  // confirmation is not worth an interruption, and a failure is.
  danger: { classes: "border-danger-line bg-danger-surface text-danger", role: "alert" },
  success: { classes: "border-success-line bg-success-surface text-success", role: "status" },
  info: { classes: "border-info-line bg-info-surface text-info", role: "status" },
};

export function Alert({
  tone,
  title,
  children,
  className,
}: {
  tone: AlertTone;
  title?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const { classes, role } = TONES[tone];
  return (
    <div
      role={role}
      className={cn("flex gap-2.5 rounded-control border p-3 text-sm", classes, className)}
    >
      {tone === "danger" ? <Icon name="warning" className="mt-px size-4" /> : null}
      <div className="min-w-0">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={cn(title && "mt-0.5")}>{children}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Write `Badge.tsx`, `Card.tsx`, `PageHeader.tsx`, `EmptyState.tsx`, `Spinner.tsx`**

`Badge.tsx`:

```tsx
import { cn } from "./cn";

export type BadgeTone = "neutral" | "success" | "warn" | "info";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-line bg-surface-muted text-ink-muted",
  success: "border-success-line bg-success-surface text-success",
  warn: "border-warn-line bg-warn-surface text-warn",
  info: "border-info-line bg-info-surface text-info",
};

export function Badge({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
  className?: string;
  /** Native tooltip. The playground's meta chips use it to name the value. */
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-control border px-2 py-0.5 text-xs font-medium",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
```

`Card.tsx`:

```tsx
import { cn } from "./cn";

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("rounded-card border border-line bg-surface", className)}>{children}</div>;
}

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description ? <p className="mt-1 text-xs text-ink-muted">{description}</p> : null}
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}

export function CardBody({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("px-5 py-4", className)}>{children}</div>;
}

export function CardFooter({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("flex items-center gap-3 border-t border-line px-5 py-3", className)}>
      {children}
    </div>
  );
}
```

`PageHeader.tsx`:

```tsx
import Link from "next/link";
import { Icon } from "./icons";

export type Crumb = { href: string; label: string };

export function PageHeader({
  title,
  description,
  meta,
  actions,
  breadcrumb,
}: {
  title: string;
  description?: string;
  /** Small facts that belong beside the title — a slug, a status badge. */
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  breadcrumb?: Crumb[];
}) {
  return (
    <header className="mb-6">
      {breadcrumb?.length ? (
        <nav aria-label="Breadcrumb" className="mb-2 flex items-center gap-1 text-xs text-ink-muted">
          {breadcrumb.map((crumb) => (
            <span key={crumb.href} className="flex items-center gap-1">
              <Link href={crumb.href} className="hover:text-ink hover:underline">
                {crumb.label}
              </Link>
              <Icon name="chevronRight" className="size-3.5" />
            </span>
          ))}
          <span className="text-ink">{title}</span>
        </nav>
      ) : null}

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
          {description ? <p className="mt-1 max-w-prose text-sm text-ink-muted">{description}</p> : null}
          {meta ? <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}
```

`EmptyState.tsx`:

```tsx
import { Icon, type IconName } from "./icons";

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: IconName;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      {icon ? (
        <span className="flex size-10 items-center justify-center rounded-card border border-line bg-surface-muted text-ink-subtle">
          <Icon name={icon} />
        </span>
      ) : null}
      <div>
        <p className="text-sm font-semibold text-ink">{title}</p>
        <p className="mx-auto mt-1 max-w-sm text-sm text-ink-muted">{description}</p>
      </div>
      {action}
    </div>
  );
}
```

`Spinner.tsx`:

```tsx
import { cn } from "./cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={cn("size-4 shrink-0 animate-spin", className)}
    >
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" opacity="0.25" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** The one way this app says it is waiting. */
export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center gap-2 px-5 py-8 text-sm text-ink-subtle">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `npm run test -- src/components/ui/Alert.test.tsx`
Expected: PASS, 3 tests.

- [ ] **Step 7: Run the gates and commit**

Run: `npm run test` (expected: **40 tests in 7 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/components/ui/
git commit -m "$(cat <<'MSG'
feat(web): add the display primitives

Card, PageHeader, Alert, Badge, EmptyState, LoadingState, and sixteen inline
icon glyphs. Between them they replace eight ad-hoc card divs, seven copies
of the red error block, and five bare "Loading…" strings.

Alert picks its ARIA role from its tone, so a save confirmation announces
itself politely as a status and a failure interrupts as an alert -- which the
hand-written copies got right only by coincidence.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 6: The pure logic layer

**Files:**
- Create: `apps/web/src/lib/graphql-errors.ts`
- Create: `apps/web/src/lib/agent-status.ts`
- Create: `apps/web/src/lib/setup-checklist.ts`
- Create: `apps/web/src/lib/chat-totals.ts`
- Test: `apps/web/src/lib/graphql-errors.test.ts`
- Test: `apps/web/src/lib/agent-status.test.ts`
- Test: `apps/web/src/lib/setup-checklist.test.ts`
- Test: `apps/web/src/lib/chat-totals.test.ts`

**Interfaces:**
- Consumes: `AgentStatus` (type-only, from `@/graphql/generated`), `CombinedError` (type-only, from `urql`), `BadgeTone` (type-only, from `@/components/ui/Badge`, Task 5).
- Produces — every page task depends on these exact signatures:
  - `firstGraphQLError(error: CombinedError | undefined): string | null`
  - `agentStatusTone(status: AgentStatus): BadgeTone` and `agentStatusLabel(status: AgentStatus): string`
  - `type ChecklistAgent = { status: AgentStatus; provider: string }`
  - `type ChecklistStep = { id: ChecklistStepId; title: string; description: string; done: boolean | null; action: { href: string; label: string } }`
  - `deriveChecklist(agents: readonly ChecklistAgent[]): ChecklistStep[]`
  - `checklistProgress(steps: readonly ChecklistStep[]): { done: number; total: number }`
  - `type TurnMeta = { usage: { input_tokens: number; output_tokens: number }; costUsd: string | null }`
  - `type SessionTotals = { inputTokens: number; outputTokens: number; costUsd: number; pricedTurns: number; unpricedTurns: number }`
  - `sessionTotals(turns: readonly TurnMeta[]): SessionTotals`

These four modules exist so that every value a page derives is a tested pure function rather than an expression buried in JSX.

- [ ] **Step 1: Write the failing tests**

`apps/web/src/lib/graphql-errors.test.ts`:

```ts
import { CombinedError } from "urql";
import { describe, expect, it } from "vitest";
import { firstGraphQLError } from "./graphql-errors";

describe("firstGraphQLError", () => {
  it("returns null when there is no error", () => {
    expect(firstGraphQLError(undefined)).toBeNull();
  });

  it("returns the first GraphQL error message", () => {
    const error = new CombinedError({ graphQLErrors: ["Agent name already taken", "second"] });
    expect(firstGraphQLError(error)).toBe("Agent name already taken");
  });

  it("explains a network failure in words a user can act on", () => {
    const error = new CombinedError({ networkError: new Error("Failed to fetch") });
    expect(firstGraphQLError(error)).toBe(
      "Could not reach the server. Check your connection and try again.",
    );
  });
});
```

`apps/web/src/lib/agent-status.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { agentStatusLabel, agentStatusTone } from "./agent-status";

describe("agent status presentation", () => {
  it("maps each status to a badge tone", () => {
    expect(agentStatusTone("ACTIVE")).toBe("success");
    expect(agentStatusTone("DRAFT")).toBe("warn");
    expect(agentStatusTone("DISABLED")).toBe("neutral");
  });

  it("renders statuses in sentence case rather than as enum shouting", () => {
    expect(agentStatusLabel("ACTIVE")).toBe("Active");
    expect(agentStatusLabel("DRAFT")).toBe("Draft");
    expect(agentStatusLabel("DISABLED")).toBe("Disabled");
  });
});
```

`apps/web/src/lib/setup-checklist.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { checklistProgress, deriveChecklist, type ChecklistAgent } from "./setup-checklist";

function stepById(agents: readonly ChecklistAgent[], id: string) {
  const step = deriveChecklist(agents).find((candidate) => candidate.id === id);
  if (!step) throw new Error(`no step ${id}`);
  return step;
}

describe("deriveChecklist", () => {
  it("has nothing done for a brand-new organization", () => {
    const steps = deriveChecklist([]);
    expect(steps.map((step) => step.done)).toEqual([false, false, false, null]);
  });

  it("completes create-agent as soon as one agent exists", () => {
    expect(stepById([{ status: "DRAFT", provider: "fake" }], "create-agent").done).toBe(true);
  });

  it("does not count the fake provider as a real one", () => {
    // DEFAULT_LLM_PROVIDER is `fake`, so this is every fresh install: an
    // assistant that only pretends to answer. The checklist has to say so.
    expect(stepById([{ status: "ACTIVE", provider: "fake" }], "real-provider").done).toBe(false);
  });

  it("completes real-provider when any agent uses a live provider", () => {
    const agents: ChecklistAgent[] = [
      { status: "DRAFT", provider: "fake" },
      { status: "DRAFT", provider: "anthropic" },
    ];
    expect(stepById(agents, "real-provider").done).toBe(true);
  });

  it("completes activate-agent only for an ACTIVE agent", () => {
    expect(stepById([{ status: "DRAFT", provider: "openai" }], "activate-agent").done).toBe(false);
    expect(stepById([{ status: "ACTIVE", provider: "openai" }], "activate-agent").done).toBe(true);
  });

  it("leaves test-agent unknowable, because the data cannot tell", () => {
    // No query exposes whether a conversation ever happened. `null` renders as
    // an action, never as a satisfied checkmark.
    expect(stepById([{ status: "ACTIVE", provider: "openai" }], "test-agent").done).toBeNull();
  });
});

describe("checklistProgress", () => {
  it("counts only the steps whose completion can be known", () => {
    expect(checklistProgress(deriveChecklist([]))).toEqual({ done: 0, total: 3 });
  });

  it("counts the completed knowable steps", () => {
    const steps = deriveChecklist([{ status: "ACTIVE", provider: "openai" }]);
    expect(checklistProgress(steps)).toEqual({ done: 3, total: 3 });
  });
});
```

`apps/web/src/lib/chat-totals.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { sessionTotals, type TurnMeta } from "./chat-totals";

const turn = (input: number, output: number, costUsd: string | null): TurnMeta => ({
  usage: { input_tokens: input, output_tokens: output },
  costUsd,
});

describe("sessionTotals", () => {
  it("is all zeroes for an empty transcript", () => {
    expect(sessionTotals([])).toEqual({
      inputTokens: 0,
      outputTokens: 0,
      costUsd: 0,
      pricedTurns: 0,
      unpricedTurns: 0,
    });
  });

  it("sums tokens and cost across turns", () => {
    const totals = sessionTotals([turn(10, 20, "0.0012"), turn(5, 7, "0.0003")]);
    expect(totals.inputTokens).toBe(15);
    expect(totals.outputTokens).toBe(27);
    expect(totals.costUsd).toBeCloseTo(0.0015, 10);
    expect(totals.pricedTurns).toBe(2);
    expect(totals.unpricedTurns).toBe(0);
  });

  it("counts an unpriced turn instead of treating it as free", () => {
    // cost_usd is null when the model is not in the pricing table. Adding it
    // as zero would report a cheap session rather than an unknown one.
    const totals = sessionTotals([turn(10, 20, "0.0012"), turn(1, 1, null)]);
    expect(totals.costUsd).toBeCloseTo(0.0012, 10);
    expect(totals.pricedTurns).toBe(1);
    expect(totals.unpricedTurns).toBe(1);
    expect(totals.inputTokens).toBe(11);
  });

  it("treats an unparseable cost as unpriced rather than as NaN", () => {
    const totals = sessionTotals([turn(1, 1, "not-a-number")]);
    expect(totals.costUsd).toBe(0);
    expect(totals.unpricedTurns).toBe(1);
  });
});
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `npm run test -- src/lib/graphql-errors.test.ts src/lib/agent-status.test.ts src/lib/setup-checklist.test.ts src/lib/chat-totals.test.ts`
Expected: FAIL — four unresolved imports.

- [ ] **Step 3: Write `graphql-errors.ts`**

```ts
import type { CombinedError } from "urql";

/**
 * The message to show a user for a failed urql operation. Replaces four
 * copies of `result.error?.graphQLErrors[0]?.message ?? null`, which rendered
 * nothing at all when the request never reached the server.
 */
export function firstGraphQLError(error: CombinedError | undefined): string | null {
  if (!error) return null;

  const graphQLMessage = error.graphQLErrors[0]?.message;
  if (graphQLMessage) return graphQLMessage;

  if (error.networkError) {
    return "Could not reach the server. Check your connection and try again.";
  }

  return error.message || "Something went wrong. Please try again.";
}
```

- [ ] **Step 4: Write `agent-status.ts`**

```ts
import type { BadgeTone } from "@/components/ui/Badge";
import type { AgentStatus } from "@/graphql/generated";

/** DRAFT is a warning, not a failure: it means "configured but not live". */
export function agentStatusTone(status: AgentStatus): BadgeTone {
  switch (status) {
    case "ACTIVE":
      return "success";
    case "DRAFT":
      return "warn";
    case "DISABLED":
      return "neutral";
  }
}

export function agentStatusLabel(status: AgentStatus): string {
  switch (status) {
    case "ACTIVE":
      return "Active";
    case "DRAFT":
      return "Draft";
    case "DISABLED":
      return "Disabled";
  }
}
```

- [ ] **Step 5: Write `setup-checklist.ts`**

```ts
import type { AgentStatus } from "@/graphql/generated";

/** Only the two fields the checklist reads, so the Agents query can change
 * shape without touching this module. */
export type ChecklistAgent = { status: AgentStatus; provider: string };

export type ChecklistStepId = "create-agent" | "real-provider" | "activate-agent" | "test-agent";

export type ChecklistStep = {
  id: ChecklistStepId;
  title: string;
  description: string;
  /** `null` when the dashboard cannot know. Rendered as an action, never a tick. */
  done: boolean | null;
  action: { href: string; label: string };
};

/** The API's `DEFAULT_LLM_PROVIDER`, which answers offline with a canned reply. */
const OFFLINE_PROVIDER = "fake";

export function deriveChecklist(agents: readonly ChecklistAgent[]): ChecklistStep[] {
  return [
    {
      id: "create-agent",
      title: "Create an agent",
      description: "An agent is one assistant, with its own model, prompt and behaviour.",
      done: agents.length > 0,
      action: { href: "/dashboard/agents", label: "Go to Agents" },
    },
    {
      id: "real-provider",
      title: "Connect a real model provider",
      description:
        "New agents start on the offline `fake` provider, which returns a canned reply and costs nothing. Switch to OpenAI or Anthropic to get real answers.",
      done: agents.some((agent) => agent.provider !== OFFLINE_PROVIDER),
      action: { href: "/dashboard/agents", label: "Choose a provider" },
    },
    {
      id: "activate-agent",
      title: "Activate an agent",
      description: "A draft agent is configurable but not yet live for your customers.",
      done: agents.some((agent) => agent.status === "ACTIVE"),
      action: { href: "/dashboard/agents", label: "Review statuses" },
    },
    {
      id: "test-agent",
      // Nothing in the schema records whether a conversation has happened, so
      // this step is never claimed as done -- an honest action beats a tick
      // that might be a lie.
      title: "Send it a test message",
      description: "Watch a real answer stream back, with its tokens, latency and cost.",
      done: null,
      action: { href: "/dashboard/playground", label: "Open playground" },
    },
  ];
}

export function checklistProgress(steps: readonly ChecklistStep[]): { done: number; total: number } {
  const knowable = steps.filter((step) => step.done !== null);
  return { done: knowable.filter((step) => step.done === true).length, total: knowable.length };
}
```

- [ ] **Step 6: Write `chat-totals.ts`**

```ts
/** The metadata a completed assistant turn carries (from the `message_end`
 * SSE event). Structural on purpose, so this module does not import a
 * component's props type. */
export type TurnMeta = {
  usage: { input_tokens: number; output_tokens: number };
  costUsd: string | null;
};

export type SessionTotals = {
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  pricedTurns: number;
  /** Turns whose model is not in the pricing table. Never folded into `costUsd`. */
  unpricedTurns: number;
};

export function sessionTotals(turns: readonly TurnMeta[]): SessionTotals {
  return turns.reduce<SessionTotals>(
    (totals, turn) => {
      const cost = turn.costUsd === null ? Number.NaN : Number(turn.costUsd);
      const priced = Number.isFinite(cost);
      return {
        inputTokens: totals.inputTokens + turn.usage.input_tokens,
        outputTokens: totals.outputTokens + turn.usage.output_tokens,
        // An unpriced turn adds nothing to the total and is counted separately:
        // reporting it as $0 would describe a cheap session, not an unknown one.
        costUsd: priced ? totals.costUsd + cost : totals.costUsd,
        pricedTurns: totals.pricedTurns + (priced ? 1 : 0),
        unpricedTurns: totals.unpricedTurns + (priced ? 0 : 1),
      };
    },
    { inputTokens: 0, outputTokens: 0, costUsd: 0, pricedTurns: 0, unpricedTurns: 0 },
  );
}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `npm run test -- src/lib/graphql-errors.test.ts src/lib/agent-status.test.ts src/lib/setup-checklist.test.ts src/lib/chat-totals.test.ts`
Expected: PASS — 3 + 2 + 8 + 4 = 17 tests.

- [ ] **Step 8: Run the gates and commit**

Run: `npm run test` (expected: **57 tests in 11 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/lib/graphql-errors.ts apps/web/src/lib/agent-status.ts apps/web/src/lib/setup-checklist.ts apps/web/src/lib/chat-totals.ts apps/web/src/lib/graphql-errors.test.ts apps/web/src/lib/agent-status.test.ts apps/web/src/lib/setup-checklist.test.ts apps/web/src/lib/chat-totals.test.ts
git commit -m "$(cat <<'MSG'
feat(web): extract the values pages derive into tested pure functions

firstGraphQLError (which now has something to say when the request never
reached the server), the agent status tone and label mapping, the Overview
setup checklist, and the playground's session token and cost totals.

Two honesty rules are encoded and tested rather than left to a renderer: the
offline `fake` provider does not satisfy "connect a real provider", and a
turn with no price is counted as unpriced instead of summed as free.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 7: Navigation data and the active-route predicate

**Files:**
- Create: `apps/web/src/components/shell/nav.ts`
- Test: `apps/web/src/components/shell/nav.test.ts`

**Interfaces:**
- Consumes: `IconName` (type-only, Task 5).
- Produces:
  - `type NavItem = { href: string; label: string; icon: IconName; state: "live" | "soon"; phase?: string }`
  - `type NavGroup = { label: string | null; items: NavItem[] }`
  - `NAV_GROUPS: NavGroup[]`
  - `isActive(pathname: string, href: string): boolean`
  - `currentSectionLabel(pathname: string): string`

- [ ] **Step 1: Write the failing test**

`apps/web/src/components/shell/nav.test.ts`. The first two cases are a regression test for a defect that exists today: `pathname === item.href` leaves an agent's detail page with nothing highlighted.

```ts
import { describe, expect, it } from "vitest";
import { currentSectionLabel, isActive, NAV_GROUPS } from "./nav";

describe("isActive", () => {
  it("keeps Agents highlighted on an agent's detail page", () => {
    expect(isActive("/dashboard/agents/0191e4c0-1234-7000-8000-000000000000", "/dashboard/agents")).toBe(
      true,
    );
  });

  it("matches a section's own page", () => {
    expect(isActive("/dashboard/agents", "/dashboard/agents")).toBe(true);
  });

  it("does not match a sibling route that merely starts with the same characters", () => {
    expect(isActive("/dashboard/agentsomething", "/dashboard/agents")).toBe(false);
  });

  it("matches Overview only exactly, since every route is under /dashboard", () => {
    expect(isActive("/dashboard", "/dashboard")).toBe(true);
    expect(isActive("/dashboard/agents", "/dashboard")).toBe(false);
  });
});

describe("currentSectionLabel", () => {
  it("names the section a nested route belongs to", () => {
    expect(currentSectionLabel("/dashboard/agents/abc")).toBe("Agents");
  });

  it("names Overview for the dashboard root", () => {
    expect(currentSectionLabel("/dashboard")).toBe("Overview");
  });

  it("falls back to a generic label for an unknown route", () => {
    expect(currentSectionLabel("/dashboard/nowhere")).toBe("Dashboard");
  });
});

describe("NAV_GROUPS", () => {
  it("gives every not-yet-built section the phase it arrives in", () => {
    const soon = NAV_GROUPS.flatMap((group) => group.items).filter((item) => item.state === "soon");
    expect(soon.length).toBeGreaterThan(0);
    expect(soon.every((item) => Boolean(item.phase))).toBe(true);
  });

  it("has no duplicate hrefs", () => {
    const hrefs = NAV_GROUPS.flatMap((group) => group.items).map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/components/shell/nav.test.ts`
Expected: FAIL — `Failed to resolve import "./nav"`.

- [ ] **Step 3: Write `nav.ts`**

```ts
import type { IconName } from "@/components/ui/icons";

export type NavItem = {
  href: string;
  label: string;
  icon: IconName;
  /** `soon` items link to a PlaceholderPage and carry a visible badge. */
  state: "live" | "soon";
  /** Which phase the section arrives in. Required for `soon` items. */
  phase?: string;
};

export type NavGroup = { label: string | null; items: NavItem[] };

/**
 * Grouped to mirror the product's loop -- configure the assistant, then run
 * it -- which is the cheapest way to make the sidebar explain the product.
 *
 * The not-yet-built sections stay in the nav deliberately (the shape of the
 * product is worth showing early), but they are badged, so a click on one is
 * an informed click rather than a dead end.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [{ href: "/dashboard", label: "Overview", icon: "overview", state: "live" }],
  },
  {
    label: "Configure",
    items: [
      { href: "/dashboard/agents", label: "Agents", icon: "agent", state: "live" },
      { href: "/dashboard/prompts", label: "Prompts", icon: "prompt", state: "soon", phase: "Phase 2" },
      {
        href: "/dashboard/knowledge",
        label: "Knowledge",
        icon: "knowledge",
        state: "soon",
        phase: "Phase 3",
      },
      { href: "/dashboard/products", label: "Products", icon: "product", state: "soon", phase: "Phase 4" },
    ],
  },
  {
    label: "Run",
    items: [
      { href: "/dashboard/playground", label: "Playground", icon: "playground", state: "live" },
      { href: "/dashboard/leads", label: "Leads", icon: "lead", state: "soon", phase: "Phase 4" },
    ],
  },
];

/**
 * `/dashboard` matches only itself, because every route lives under it.
 * Everything else matches its own page and anything nested below it -- the
 * trailing slash is what keeps `/dashboard/agentsomething` from matching
 * `/dashboard/agents`.
 */
export function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard") return pathname === "/dashboard";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function currentSectionLabel(pathname: string): string {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (isActive(pathname, item.href)) return item.label;
    }
  }
  return "Dashboard";
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm run test -- src/components/shell/nav.test.ts`
Expected: PASS, 9 tests.

- [ ] **Step 5: Run the gates and commit**

Run: `npm run test` (expected: **66 tests in 12 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/components/shell/nav.ts apps/web/src/components/shell/nav.test.ts
git commit -m "$(cat <<'MSG'
feat(web): make the nav data and its active-route rule testable

The sidebar's `pathname === item.href` check meant an agent's detail page
highlighted nothing, leaving the user nowhere in the nav. isActive now
prefix-matches on a path-segment boundary, with a regression test for both
that case and the /dashboard/agentsomething false positive it must not cause.

The nav becomes grouped data -- Configure, then Run -- with each not-yet-built
section carrying the phase it arrives in.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 8: The responsive dashboard shell

**Files:**
- Create: `apps/web/src/components/shell/Sidebar.tsx`
- Create: `apps/web/src/components/shell/TopBar.tsx`
- Rewrite: `apps/web/src/app/dashboard/layout.tsx` (all 67 lines)

**Interfaces:**
- Consumes: `NAV_GROUPS`, `isActive`, `currentSectionLabel` (Task 7); `Badge`, `Icon`, `LoadingState` (Task 5); `Button` (Task 3); `useAuth` (existing, unchanged).
- Produces:
  - `Sidebar({ organizationName, email, onSignOut, signingOut, onNavigate? })` — `onNavigate` fires on any nav click so the mobile drawer can close itself.
  - `TopBar({ sectionLabel, onOpenNav })`
  - A layout whose `<main id="main">` is the app's only vertical scroll container. **Every later page task depends on this**: a page may use `h-full` and scroll internally without any viewport arithmetic.

- [ ] **Step 1: Write `Sidebar.tsx`**

```tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";
import { isActive, NAV_GROUPS } from "./nav";

export function Sidebar({
  organizationName,
  email,
  onSignOut,
  signingOut = false,
  onNavigate,
}: {
  organizationName: string;
  email: string;
  onSignOut: () => void;
  signingOut?: boolean;
  /** Called on any nav click, so the mobile drawer can close itself. */
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col border-r border-line bg-surface">
      <div className="flex h-14 shrink-0 items-center gap-2 border-b border-line px-4">
        <span className="flex size-7 items-center justify-center rounded-control bg-primary text-primary-ink">
          <Icon name="agent" className="size-4" />
        </span>
        <span className="truncate text-sm font-semibold text-ink">AI Sales Agent</span>
      </div>

      <nav aria-label="Main" className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
        {NAV_GROUPS.map((group) => (
          <div key={group.label ?? "root"} className="mb-4 last:mb-0">
            {group.label ? (
              <p className="px-3 pb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                {group.label}
              </p>
            ) : null}
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const active = isActive(pathname, item.href);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={onNavigate}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex items-center gap-2.5 rounded-control px-3 py-2 text-sm transition-colors",
                        active
                          ? "bg-primary font-medium text-primary-ink"
                          : "text-ink-muted hover:bg-surface-muted hover:text-ink",
                      )}
                    >
                      <Icon name={item.icon} className="size-4" />
                      <span className="flex-1 truncate">{item.label}</span>
                      {/* Badged rather than hidden: the shape of the product is
                       * worth showing early, but a click should be informed. */}
                      {item.state === "soon" ? (
                        <Badge tone={active ? "neutral" : "warn"} className="shrink-0">
                          Soon
                        </Badge>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="shrink-0 border-t border-line p-3">
        <p className="truncate px-1 text-sm font-medium text-ink">{organizationName}</p>
        <p className="truncate px-1 text-xs text-ink-subtle">{email}</p>
        <Button
          variant="secondary"
          size="sm"
          onClick={onSignOut}
          loading={signingOut}
          loadingLabel="Signing out…"
          className="mt-3 w-full"
        >
          Sign out
        </Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write `TopBar.tsx`**

```tsx
"use client";

import { Icon } from "@/components/ui/icons";

/** Mobile only. On `lg` and up the sidebar is always visible, so a second
 * header would just cost vertical space the playground wants. */
export function TopBar({
  sectionLabel,
  onOpenNav,
}: {
  sectionLabel: string;
  onOpenNav: () => void;
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface px-4 lg:hidden">
      <button
        type="button"
        onClick={onOpenNav}
        aria-label="Open navigation"
        className="-ml-1 rounded-control p-1.5 text-ink-muted hover:bg-surface-muted hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink"
      >
        <Icon name="menu" />
      </button>
      <span className="truncate text-sm font-semibold text-ink">{sectionLabel}</span>
    </header>
  );
}
```

- [ ] **Step 3: Rewrite `dashboard/layout.tsx`**

Replace the whole file:

```tsx
"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Sidebar } from "@/components/shell/Sidebar";
import { TopBar } from "@/components/shell/TopBar";
import { currentSectionLabel } from "@/components/shell/nav";
import { LoadingState } from "@/components/ui/Spinner";
import { cn } from "@/components/ui/cn";
import { useAuth } from "@/lib/auth";

/** Routes that fill the frame and manage their own internal scrolling, rather
 * than sitting in the centred content well. The playground's transcript is
 * the scroll container, which is what lets it drop the viewport arithmetic. */
const FULL_BLEED_ROUTES = new Set(["/dashboard/playground"]);

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [navOpen, setNavOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  // Navigating is the drawer's natural dismissal: the user got where they
  // were going, and leaving it open would cover the page they asked for.
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setNavOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navOpen]);

  async function onSignOut() {
    setSigningOut(true);
    try {
      await logout();
      router.replace("/login");
    } finally {
      setSigningOut(false);
    }
  }

  if (loading) return <LoadingState label="Loading your workspace…" />;
  if (!user) return null;

  const fullBleed = FULL_BLEED_ROUTES.has(pathname);

  return (
    // h-dvh + a single overflow-y-auto main is what removes the need for any
    // page to compute a height from the viewport.
    <div className="flex h-dvh overflow-hidden">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-control focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:text-primary-ink"
      >
        Skip to content
      </a>

      <div className="hidden w-64 shrink-0 lg:block">
        <Sidebar
          organizationName={user.organization_name}
          email={user.email}
          onSignOut={onSignOut}
          signingOut={signingOut}
        />
      </div>

      {navOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
            className="absolute inset-0 bg-ink/40"
          />
          <div className="absolute left-0 top-0 h-full w-64">
            <Sidebar
              organizationName={user.organization_name}
              email={user.email}
              onSignOut={onSignOut}
              signingOut={signingOut}
              onNavigate={() => setNavOpen(false)}
            />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar sectionLabel={currentSectionLabel(pathname)} onOpenNav={() => setNavOpen(true)} />
        <main id="main" className={cn("min-h-0 flex-1 overflow-y-auto", !fullBleed && "px-6 py-8")}>
          {fullBleed ? children : <div className="mx-auto w-full max-w-6xl">{children}</div>}
        </main>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run the gates**

Run: `npm run test` (expected: still **66 tests in 12 files** — this task adds no tests), then `npm run typecheck`, then `npm run lint`.
Expected: all clean.

- [ ] **Step 5: Verify in the browser**

Run `npm run dev`, sign in, and confirm all of the following:

1. At desktop width the sidebar shows the brand, `Overview`, then a **Configure** group and a **Run** group, with `Soon` badges on Prompts, Knowledge, Products and Leads.
2. Navigate to an agent's detail page (`/dashboard/agents/<id>`) — **Agents stays highlighted.** This is the defect Task 7 fixed; confirm it visibly.
3. Narrow the window below 1024px: the sidebar disappears and a header with a hamburger appears. Open the drawer, click a nav item — it navigates *and* closes. Reopen it and press `Escape` — it closes. Reopen and click the dark backdrop — it closes.
4. Press `Tab` from a fresh page load: the first stop is a visible **Skip to content** button.
5. The account block (org name, email, Sign out) is at the bottom of the sidebar.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/shell/Sidebar.tsx apps/web/src/components/shell/TopBar.tsx apps/web/src/app/dashboard/layout.tsx
git commit -m "$(cat <<'MSG'
feat(web): rebuild the dashboard shell, responsive and grouped

The 67-line monolith becomes an auth guard plus a frame: Sidebar owns the
grouped nav and the account block, TopBar owns the mobile hamburger. Below
lg the sidebar is an off-canvas drawer that closes on navigation, Escape,
or a backdrop click, which the fixed w-60 aside had no answer for.

The frame is h-dvh with a single scrolling <main>, so a page can say h-full
instead of guessing at h-[calc(100vh-4rem)]. A skip link is now the first
tab stop.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 9: The auth pages

**Files:**
- Create: `apps/web/src/app/(auth)/layout.tsx`
- Rewrite: `apps/web/src/app/(auth)/login/page.tsx`
- Rewrite: `apps/web/src/app/(auth)/register/page.tsx`

**Interfaces:**
- Consumes: `Card`/`CardBody` (Task 5), `Field`/`Input` (Task 4), `Button` (Task 3), `Alert` (Task 5), `useAuth` (existing, unchanged).
- Produces: nothing other tasks consume.

Note: these two forms catch an `ApiError` thrown by `apiFetch`, **not** a urql `CombinedError`, so they keep their existing `catch` logic and do not use `firstGraphQLError`.

- [ ] **Step 1: Create the shared auth frame**

`apps/web/src/app/(auth)/layout.tsx`:

```tsx
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-6 px-6 py-12">
      {/* The only place a new user meets the product, and previously a bare
       * form on a white page. One sentence is cheap and orients them. */}
      <div className="text-center">
        <h1 className="text-lg font-semibold tracking-tight text-ink">AI Sales Agent</h1>
        <p className="mx-auto mt-1 max-w-sm text-sm text-ink-muted">
          Configure an AI assistant over your own products and documents, then let it talk to your
          customers.
        </p>
      </div>
      <div className="w-full max-w-sm">{children}</div>
    </main>
  );
}
```

- [ ] **Step 2: Rewrite the login page**

Replace the `return (…)` block of `apps/web/src/app/(auth)/login/page.tsx` (everything from `return (` to the closing `);`) with the following, and replace the import block at the top. The `onSubmit` handler and all four `useState` calls stay exactly as they are.

New imports:

```tsx
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useAuth } from "@/lib/auth";
```

New render:

```tsx
  return (
    <Card>
      <CardBody className="space-y-5 p-6">
        <div>
          <h2 className="text-base font-semibold text-ink">Sign in</h2>
          <p className="mt-0.5 text-sm text-ink-muted">Welcome back.</p>
        </div>

        {error ? <Alert tone="danger">{error}</Alert> : null}

        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Email" required>
            {(control) => (
              <Input
                {...control}
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            )}
          </Field>

          <Field label="Password" required>
            {(control) => (
              <Input
                {...control}
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          <Button
            type="submit"
            loading={submitting}
            loadingLabel="Signing in…"
            className="w-full"
          >
            Sign in
          </Button>
        </form>

        <p className="text-center text-sm text-ink-muted">
          No account?{" "}
          <Link href="/register" className="font-medium text-ink underline">
            Create one
          </Link>
        </p>
      </CardBody>
    </Card>
  );
```

- [ ] **Step 3: Rewrite the register page**

Same treatment for `apps/web/src/app/(auth)/register/page.tsx` — identical import block (plus nothing extra), `onSubmit` and all six `useState` calls unchanged, and this render:

```tsx
  return (
    <Card>
      <CardBody className="space-y-5 p-6">
        <div>
          <h2 className="text-base font-semibold text-ink">Create your workspace</h2>
          <p className="mt-0.5 text-sm text-ink-muted">
            One workspace per business. You can invite people later.
          </p>
        </div>

        {error ? <Alert tone="danger">{error}</Alert> : null}

        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Full name" required>
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
            )}
          </Field>

          <Field
            label="Organization name"
            description="Shown to your team. Your agents and data belong to it."
            required
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="organization"
                value={organizationName}
                onChange={(e) => setOrganizationName(e.target.value)}
              />
            )}
          </Field>

          <Field label="Email" required>
            {(control) => (
              <Input
                {...control}
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            )}
          </Field>

          {/* The form has always enforced minLength={12} and never said so,
           * so the only way to learn it was to be rejected. */}
          <Field label="Password" description="At least 12 characters." required>
            {(control) => (
              <Input
                {...control}
                type="password"
                autoComplete="new-password"
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          <Button
            type="submit"
            loading={submitting}
            loadingLabel="Creating…"
            className="w-full"
          >
            Create workspace
          </Button>
        </form>

        <p className="text-center text-sm text-ink-muted">
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-ink underline">
            Sign in
          </Link>
        </p>
      </CardBody>
    </Card>
  );
```

- [ ] **Step 4: Run the gates**

Run: `npm run test` (expected: **66 tests in 12 files**), `npm run typecheck`, `npm run lint`.

- [ ] **Step 5: Verify in the browser**

With `npm run dev`:
1. `/login` and `/register` both show the product name and its one-line description above the card.
2. Submit `/login` with a wrong password — the error renders in a bordered danger `Alert`, and the button shows `Signing in…` while in flight.
3. On `/register`, the password field visibly says "At least 12 characters."
4. Click each field's **label text** — focus moves into the control (proof the `Field` wiring is real).

- [ ] **Step 6: Commit**

```bash
git add "apps/web/src/app/(auth)"
git commit -m "$(cat <<'MSG'
feat(web): give the auth pages a frame and real field wiring

A shared (auth) layout states what the product does above the card -- this
is the only screen a new user meets it on, and it was a bare form on a white
page. Both forms move to Field/Input/Button/Alert, so clicking a label now
focuses its control, and the register form finally admits the 12-character
minimum it has always enforced silently.

The ApiError catch logic in both handlers is unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 10: Overview

**Files:**
- Rewrite: `apps/web/src/app/dashboard/page.tsx` (all 28 lines)

**Interfaces:**
- Consumes: `deriveChecklist`, `checklistProgress` (Task 6); `agentStatusTone`, `agentStatusLabel` (Task 6); `PageHeader`, `Card`, `CardHeader`, `CardBody`, `Badge`, `EmptyState`, `LoadingState`, `Icon` (Task 5); `ButtonLink` (Task 3); the existing `AgentsDocument` query.
- Produces: nothing other tasks consume.

No new GraphQL: every number on this page comes from the `Agents` query the old page already ran.

- [ ] **Step 1: Write the page**

Replace the whole of `apps/web/src/app/dashboard/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useQuery } from "urql";
import { ButtonLink } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { checklistProgress, deriveChecklist } from "@/lib/setup-checklist";

const PHASES_AHEAD = [
  "Phase 3 — Knowledge: upload documents and let the agent answer from them.",
  "Phase 4 — Products, tools and lead capture.",
  "Phase 5 — Evaluation, MCP and billing.",
];

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching, error }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });

  const agents = data?.agents ?? [];
  const steps = deriveChecklist(agents);
  const progress = checklistProgress(steps);
  const activeCount = agents.filter((agent) => agent.status === "ACTIVE").length;
  const draftCount = agents.filter((agent) => agent.status === "DRAFT").length;

  if (fetching && !data) return <LoadingState label="Loading your workspace…" />;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description={`What ${user?.organization_name ?? "your workspace"} has set up, and what is left to do.`}
      />

      {error ? (
        <Card>
          <CardBody>
            <p className="text-sm text-danger">
              Could not load your agents. Reload the page to try again.
            </p>
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader
          title="Get your assistant ready"
          description={`${progress.done} of ${progress.total} steps done.`}
        />
        <ul className="divide-y divide-line">
          {steps.map((step) => (
            <li key={step.id} className="flex items-start gap-3 px-5 py-4">
              <span
                className={
                  step.done === true
                    ? "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-control bg-success-surface text-success"
                    : "mt-0.5 flex size-5 shrink-0 items-center justify-center text-ink-subtle"
                }
              >
                <Icon name={step.done === true ? "check" : "circle"} className="size-4" />
              </span>
              <div className="min-w-0 flex-1">
                <p
                  className={
                    step.done === true
                      ? "text-sm font-medium text-ink-muted line-through"
                      : "text-sm font-medium text-ink"
                  }
                >
                  {step.title}
                </p>
                <p className="mt-0.5 text-sm text-ink-muted">{step.description}</p>
              </div>
              {step.done === true ? null : (
                <ButtonLink href={step.action.href} variant="secondary" size="sm" className="shrink-0">
                  {step.action.label}
                </ButtonLink>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: "Agents", value: agents.length },
          { label: "Active", value: activeCount },
          { label: "Draft", value: draftCount },
        ].map((tile) => (
          <Card key={tile.label}>
            <CardBody>
              <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
                {tile.label}
              </p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-ink">{tile.value}</p>
            </CardBody>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader
          title="Your agents"
          actions={
            <ButtonLink href="/dashboard/agents" variant="secondary" size="sm">
              All agents
            </ButtonLink>
          }
        />
        {agents.length === 0 ? (
          <EmptyState
            icon="agent"
            title="No agents yet"
            description="An agent is one assistant, with its own model, prompt and behaviour."
            action={<ButtonLink href="/dashboard/agents">Create your first agent</ButtonLink>}
          />
        ) : (
          <ul className="divide-y divide-line">
            {agents.slice(0, 5).map((agent) => (
              <li key={String(agent.id)} className="flex items-center gap-3 px-5 py-3">
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/dashboard/agents/${String(agent.id)}`}
                    className="text-sm font-medium text-ink hover:underline"
                  >
                    {agent.name}
                  </Link>
                  <p className="truncate text-xs text-ink-subtle">
                    {agent.provider} · {agent.model}
                  </p>
                </div>
                <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
                <Link
                  href={`/dashboard/playground?agentId=${String(agent.id)}`}
                  className="shrink-0 text-sm text-ink-muted hover:text-ink hover:underline"
                >
                  Test
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader title="What's next" description="Sections the navigation already shows." />
        <CardBody>
          <ul className="space-y-1.5 text-sm text-ink-muted">
            {PHASES_AHEAD.map((phase) => (
              <li key={phase}>{phase}</li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Run the gates**

Run: `npm run test` (expected: **66 tests in 12 files**), `npm run typecheck`, `npm run lint`.

- [ ] **Step 3: Verify in the browser**

With `npm run dev` at `/dashboard`:
1. The checklist shows four steps with a `N of 3 steps done` count — **3, not 4**, because "Send it a test message" is not knowable from the data.
2. With only default agents present, **"Connect a real model provider" is unticked** even if an agent is Active, and its action button reads `Choose a provider`. This is the page's whole point.
3. Completed steps show a check on a green surface and a struck-through title, and their action button is gone.
4. The three tiles read Agents / Active / Draft with matching counts.
5. With zero agents, "Your agents" shows the empty state and a `Create your first agent` button.

- [ ] **Step 4: Commit**

```bash
git add apps/web/src/app/dashboard/page.tsx
git commit -m "$(cat <<'MSG'
feat(web): rebuild Overview around a real setup checklist

The page was one card counting agents. It now answers "is my assistant
ready, and what is next": a four-step checklist derived from the Agents
query the old page already ran, three counts, the first five agents with
their status, and the phases still ahead.

The checklist says out loud that a new agent runs on the offline `fake`
provider, which every fresh install starts on and nothing in the UI
previously mentioned. The step it cannot verify from the schema renders as
an action rather than a tick.

No new query, no API change.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 11: The agents list

**Files:**
- Rewrite: `apps/web/src/app/dashboard/agents/page.tsx` (all 106 lines)

**Interfaces:**
- Consumes: `firstGraphQLError`, `agentStatusTone`, `agentStatusLabel` (Task 6); `PageHeader`, `Card`, `EmptyState`, `Badge`, `LoadingState`, `Icon` (Task 5); `Button` (Task 3); `Field`/`Input` (Task 4); existing `AgentsDocument` / `CreateAgentDocument`.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Write the page**

```tsx
"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "urql";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Icon } from "@/components/ui/icons";
import { Input } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument, CreateAgentDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";

export default function AgentsPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching }, refetchAgents] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const [createResult, createAgent] = useMutation(CreateAgentDocument);
  const [name, setName] = useState("");
  // The list is what you came for, so the create form is disclosed rather
  // than parked above it permanently.
  const [creating, setCreating] = useState(false);

  const agents = data?.agents ?? [];
  const createError = firstGraphQLError(createResult.error);

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    const result = await createAgent({ name });
    if (!result.error) {
      setName("");
      setCreating(false);
      refetchAgents({ requestPolicy: "network-only" });
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agents"
        description="Each agent is one assistant, with its own model, prompt and behaviour."
        actions={
          creating ? null : (
            <Button onClick={() => setCreating(true)}>
              <Icon name="plus" className="size-4" />
              New agent
            </Button>
          )
        }
      />

      {creating ? (
        <Card>
          <form onSubmit={onCreate} className="space-y-4 p-5">
            <Field
              label="Agent name"
              description="Used to generate the agent's slug. You can change the name later."
              error={createError}
              required
            >
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              )}
            </Field>
            <div className="flex items-center gap-2">
              <Button type="submit" loading={createResult.fetching} loadingLabel="Creating…">
                Create agent
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setCreating(false);
                  setName("");
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        </Card>
      ) : null}

      <Card>
        {fetching && !data ? (
          <LoadingState label="Loading agents…" />
        ) : agents.length === 0 ? (
          <EmptyState
            icon="agent"
            title="No agents yet"
            description="Create one to configure a model, a prompt and a tone — then test it in the playground."
            action={<Button onClick={() => setCreating(true)}>Create your first agent</Button>}
          />
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
              <tr>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Name
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Slug
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Status
                </th>
                <th scope="col" className="px-5 py-2.5 font-medium">
                  Model
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {agents.map((agent) => (
                <tr key={String(agent.id)}>
                  <td className="px-5 py-3">
                    <Link
                      href={`/dashboard/agents/${String(agent.id)}`}
                      className="font-medium text-ink hover:underline"
                    >
                      {agent.name}
                    </Link>
                  </td>
                  <td className="px-5 py-3 font-mono text-xs text-ink-muted">{agent.slug}</td>
                  <td className="px-5 py-3">
                    <Badge tone={agentStatusTone(agent.status)}>
                      {agentStatusLabel(agent.status)}
                    </Badge>
                  </td>
                  <td className="px-5 py-3 text-ink-muted">
                    {agent.provider} · {agent.model}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Run the gates**

Run: `npm run test` (expected: **66 tests in 12 files**), `npm run typecheck`, `npm run lint`.

- [ ] **Step 3: Verify in the browser**

1. `/dashboard/agents` opens on the list, with `New agent` in the header.
2. Clicking `New agent` discloses the form with the name field already focused; `Cancel` hides it and clears the value.
3. Create an agent with a name that already exists — the error appears **under the name field**, in red, and the field is outlined in red (`aria-invalid` styling), not floating below the card.
4. A successful create closes the form and the new row appears.
5. Statuses render as badges (`Draft` in amber, `Active` in green), and the slug is monospaced.

- [ ] **Step 4: Commit**

```bash
git add apps/web/src/app/dashboard/agents/page.tsx
git commit -m "$(cat <<'MSG'
feat(web): open the agents page on the list, not the create form

The create form becomes a disclosure behind a New agent action, so the page
opens on the thing you came for. A create failure now renders under the
field that caused it instead of floating between the form and the table, and
statuses read as badges rather than as raw enum values.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 12: Agent detail

**Files:**
- Rewrite: `apps/web/src/app/dashboard/agents/[id]/page.tsx` (all 332 lines)

**Interfaces:**
- Consumes: `firstGraphQLError`, `agentStatusTone`, `agentStatusLabel` (Task 6); `PageHeader`, `Card`, `CardHeader`, `CardBody`, `CardFooter`, `Badge`, `Alert`, `LoadingState` (Task 5); `Button`, `ButtonLink` (Task 3); `Field`, `Input`, `Select` (Task 4); existing `AgentDocument` / `UpdateAgentDocument` / `UpdateAgentConfigDocument` / `DeleteAgentDocument`.
- Produces: nothing other tasks consume.

**All state, effects, and the three submit handlers keep their current logic.** What changes is the render, the field help text, and where the "Saved" indicator lives. The `PROVIDERS` constant and its explanatory comment are kept verbatim — it exists because `DEFAULT_LLM_PROVIDER` is `fake` and dropping `fake` from the list made the select render blank for every fresh agent.

- [ ] **Step 1: Replace the import block**

```tsx
"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "urql";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Select } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  AgentDocument,
  AgentStatus,
  DeleteAgentDocument,
  UpdateAgentConfigDocument,
  UpdateAgentDocument,
} from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
```

`next/link` is no longer imported: `PageHeader`'s breadcrumb and `ButtonLink` cover both of the page's links.

- [ ] **Step 2: Add the model suggestion table**

Above the component, alongside the existing `STATUSES` and `PROVIDERS` constants:

```tsx
// Mirrors `MODEL_PRICING` in `apps/api/app/llm/pricing.py`: the models the API
// can both run and cost. The input stays free text -- the API validates the
// model per provider -- but a datalist makes a valid id guessable instead of
// something you have to already know.
const MODEL_SUGGESTIONS: Record<string, string[]> = {
  fake: ["fake-1"],
  openai: ["gpt-4o-mini", "gpt-4o"],
  anthropic: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
};
```

- [ ] **Step 3: Replace the loading and error branches**

```tsx
  if (fetching && !agent) return <LoadingState label="Loading agent…" />;

  if (error && !agent) {
    return (
      <Alert tone="danger" title="Could not load this agent">
        {firstGraphQLError(error) ?? "Please reload the page."}
      </Alert>
    );
  }

  if (!agent) return null;
```

- [ ] **Step 4: Replace the render with four explained cards**

Identity and Model are two cards inside one `<form>`, because they are saved by the one `updateAgent` mutation — so the save button lives in the second card's footer and the first card's header says so.

```tsx
  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        breadcrumb={[{ href: "/dashboard/agents", label: "Agents" }]}
        title={agent.name}
        meta={
          <>
            <Badge tone={agentStatusTone(agent.status)}>{agentStatusLabel(agent.status)}</Badge>
            <span className="font-mono text-xs text-ink-subtle">{agent.slug}</span>
          </>
        }
        actions={
          <ButtonLink href={`/dashboard/playground?agentId=${String(agent.id)}`}>
            Test in playground
          </ButtonLink>
        }
      />

      <form onSubmit={onSubmitAgent} className="space-y-6">
        <Card>
          <CardHeader
            title="Identity"
            description="What this agent is called, and whether it is live. Saved together with Model, below."
          />
          <CardBody className="space-y-4">
            {agentError ? <Alert tone="danger">{agentError}</Alert> : null}

            <Field label="Name" required>
              {(control) => (
                <Input {...control} type="text" value={name} onChange={(e) => setName(e.target.value)} />
              )}
            </Field>

            <Field
              label="Status"
              description="Draft is configurable but not live. Disabled stops it answering."
            >
              {(control) => (
                <Select
                  {...control}
                  value={status}
                  onChange={(e) => setStatus(e.target.value as AgentStatus)}
                >
                  {STATUSES.map((option) => (
                    <option key={option} value={option}>
                      {agentStatusLabel(option)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Model" description="Which model answers, and how freely." />
          <CardBody className="space-y-4">
            <Field
              label="Provider"
              description="`fake` answers offline with a canned reply and costs nothing — useful for wiring, useless for real answers. Switch to openai or anthropic once the matching API key is set."
            >
              {(control) => (
                <Select {...control} value={provider} onChange={(e) => setProvider(e.target.value)}>
                  {PROVIDERS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field
              label="Model"
              description="Must be a model the selected provider offers. Suggestions come from the API's pricing table."
              required
            >
              {(control) => (
                <>
                  <Input
                    {...control}
                    type="text"
                    list={`${control.id}-models`}
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                  />
                  <datalist id={`${control.id}-models`}>
                    {(MODEL_SUGGESTIONS[provider] ?? []).map((option) => (
                      <option key={option} value={option} />
                    ))}
                  </datalist>
                </>
              )}
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label="Temperature"
                description="0 is repeatable, 2 is loose. Some models (claude-opus-5, claude-sonnet-5) reject it and the API drops it for them."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={temperature}
                    onChange={(e) => setTemperature(Number(e.target.value))}
                  />
                )}
              </Field>

              <Field label="Max tokens" description="Ceiling on one reply's length." required>
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={32000}
                    step={1}
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(Number(e.target.value))}
                  />
                )}
              </Field>
            </div>
          </CardBody>

          {/* In the footer, so a save no longer shifts the form under the
           * cursor the way an inserted banner did. */}
          <CardFooter>
            <Button type="submit" loading={updateAgentResult.fetching} loadingLabel="Saving…">
              Save agent
            </Button>
            {agentSaved ? (
              <span role="status" className="text-sm font-medium text-success">
                Saved
              </span>
            ) : null}
          </CardFooter>
        </Card>
      </form>

      <form onSubmit={onSubmitConfig}>
        <Card>
          <CardHeader
            title="Behaviour"
            description="How the agent speaks, and how hard it works on one answer."
          />
          <CardBody className="space-y-4">
            {configError ? <Alert tone="danger">{configError}</Alert> : null}

            <Field
              label="Tone"
              description="Folded into the system prompt — for example “direct and factual”."
              required
            >
              {(control) => (
                <Input {...control} type="text" value={tone} onChange={(e) => setTone(e.target.value)} />
              )}
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              {/* Saving a value that does nothing yet is fine. Not saying so is not. */}
              <Field
                label="Retrieval top-K"
                description="How many knowledge chunks to retrieve. Takes effect in Phase 3 (retrieval)."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={50}
                    step={1}
                    value={retrievalTopK}
                    onChange={(e) => setRetrievalTopK(Number(e.target.value))}
                  />
                )}
              </Field>

              <Field
                label="Max agent steps"
                description="Tool-calling rounds per answer. Takes effect in Phase 4 (tools)."
                required
              >
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={20}
                    step={1}
                    value={maxAgentSteps}
                    onChange={(e) => setMaxAgentSteps(Number(e.target.value))}
                  />
                )}
              </Field>
            </div>
          </CardBody>

          <CardFooter>
            <Button type="submit" loading={updateConfigResult.fetching} loadingLabel="Saving…">
              Save behaviour
            </Button>
            {configSaved ? (
              <span role="status" className="text-sm font-medium text-success">
                Saved
              </span>
            ) : null}
          </CardFooter>
        </Card>
      </form>

      <Card className="border-danger-line">
        <CardHeader
          title="Delete this agent"
          description="Its conversations and configuration go with it. This cannot be undone."
        />
        <CardBody className="space-y-3">
          {deleteError ? <Alert tone="danger">{deleteError}</Alert> : null}
          <Button
            variant="danger"
            onClick={onDelete}
            loading={deleteResult.fetching}
            loadingLabel="Deleting…"
          >
            Delete agent
          </Button>
        </CardBody>
      </Card>
    </div>
  );
```

- [ ] **Step 5: Switch the three error reads to the shared helper**

Replace the three lines that currently end in `?.graphQLErrors[0]?.message ?? null`:

```tsx
  const agentError = firstGraphQLError(updateAgentResult.error);
  const configError = firstGraphQLError(updateConfigResult.error);
  const deleteError = firstGraphQLError(deleteResult.error);
```

- [ ] **Step 6: Run the gates**

Run: `npm run test` (expected: **66 tests in 12 files**), `npm run typecheck`, `npm run lint`.

- [ ] **Step 7: Verify in the browser**

1. The header shows `Agents ›` breadcrumb, the agent name, a status badge and the monospaced slug, with `Test in playground` on the right.
2. **Every field has help text under its label.** Specifically: Provider explains what `fake` does, Temperature warns that some models reject it, and Retrieval top-K / Max agent steps each say which phase they start working in.
3. Focus the Model input — a dropdown of suggestions appears, and **it changes when you change Provider** (pick `anthropic`, see the claude ids; pick `openai`, see the gpt ids).
4. Save the Identity/Model card — `Saved` appears next to the button in the footer and **the form does not jump**.
5. The delete card is outlined in red.
6. The Identity card's header says it is saved together with Model, and the single `Save agent` button sits in the Model card's footer.

- [ ] **Step 8: Commit**

```bash
git add "apps/web/src/app/dashboard/agents/[id]/page.tsx"
git commit -m "$(cat <<'MSG'
feat(web): explain the agent form instead of just listing its fields

Nine unlabelled fields in one flat stack become four titled groups where
every field says what it does. Three of those notes are things the UI knew
and never said: `fake` answers offline for free, claude-opus-5 and
claude-sonnet-5 reject temperature, and retrieval top-K and max agent steps
do nothing until Phases 3 and 4.

The Model input gains a per-provider datalist sourced from the API's pricing
table, and the "Saved" indicator moves into the card footer so confirming a
save no longer shifts the form under the cursor.

State and submit handlers are unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 13: `ChatMessage`

**Files:**
- Rewrite: `apps/web/src/components/chat/ChatMessage.tsx` (all 78 lines)
- Test: `apps/web/src/components/chat/ChatMessage.test.tsx`

**Interfaces:**
- Consumes: `Badge` (Task 5), `Alert` (Task 5), `cn` (Task 1).
- Produces: **the exported types must not change** — `ChatMessageMeta`, `ChatMessageError`, `ChatMessageData`, and the `ChatMessage({ message })` component. The playground imports all of them and Task 14 assumes they are identical.

- [ ] **Step 1: Write the failing test**

`apps/web/src/components/chat/ChatMessage.test.tsx`. The two behaviours worth asserting are the ones a careless rewrite would lose: the honest `"not priced"` label, and the placeholder for a turn stopped before any token arrived.

```tsx
// @vitest-environment happy-dom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChatMessage, type ChatMessageData } from "./ChatMessage";

const assistant = (overrides: Partial<ChatMessageData> = {}): ChatMessageData => ({
  id: "m1",
  role: "assistant",
  text: "Our starter plan is $19 a month.",
  status: "done",
  ...overrides,
});

describe("ChatMessage", () => {
  it("says a turn is not priced rather than showing it as free", () => {
    render(
      <ChatMessage
        message={assistant({
          meta: {
            model: "some-new-model",
            usage: { input_tokens: 12, output_tokens: 34 },
            costUsd: null,
            latencyMs: 820,
          },
        })}
      />,
    );
    expect(screen.getByText("not priced")).toBeInTheDocument();
  });

  it("formats a known cost in dollars", () => {
    render(
      <ChatMessage
        message={assistant({
          meta: {
            model: "gpt-4o-mini",
            usage: { input_tokens: 12, output_tokens: 34 },
            costUsd: "0.0012",
            latencyMs: 820,
          },
        })}
      />,
    );
    expect(screen.getByText("$0.0012")).toBeInTheDocument();
  });

  it("explains an assistant turn that was stopped before any token arrived", () => {
    render(<ChatMessage message={assistant({ text: "" })} />);
    expect(screen.getByText(/Stopped before any response arrived/)).toBeInTheDocument();
  });

  it("shows a turn error as an alert while keeping the partial text", () => {
    render(
      <ChatMessage
        message={assistant({
          text: "Our starter plan",
          status: "error",
          error: { code: "provider_error", message: "The provider timed out." },
        })}
      />,
    );
    expect(screen.getByText("Our starter plan")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The provider timed out.");
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npm run test -- src/components/chat/ChatMessage.test.tsx`
Expected: FAIL — the current component renders the cost inside one run-on paragraph, so `getByText("not priced")` finds no element with that exact text.

- [ ] **Step 3: Rewrite the component**

```tsx
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/components/ui/cn";
import type { ChatUsage } from "@/lib/sse";

export type ChatMessageMeta = {
  model: string;
  usage: ChatUsage;
  costUsd: string | null;
  latencyMs: number;
};

export type ChatMessageError = {
  code: string;
  message: string;
};

export type ChatMessageData = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status: "streaming" | "done" | "error";
  error?: ChatMessageError;
  meta?: ChatMessageMeta;
};

/** `cost_usd` is `null` whenever the model isn't in the pricing table (see
 * `docs/PHASE-2.md` §5) -- that's a real, honest state, not zero cost. */
function formatCost(costUsd: string | null): string {
  if (costUsd === null) return "not priced";
  const value = Number(costUsd);
  if (Number.isNaN(value)) return "not priced";
  return `$${value.toFixed(4)}`;
}

/** One fact per chip, each labelled, so the numbers can be read at a glance
 * instead of parsed out of a sentence. */
function MetaChips({ meta }: { meta: ChatMessageMeta }) {
  const chips = [
    { label: "Model", value: meta.model },
    { label: "Tokens", value: `${meta.usage.input_tokens} in / ${meta.usage.output_tokens} out` },
    { label: "Cost", value: formatCost(meta.costUsd) },
    { label: "Latency", value: `${meta.latencyMs}ms` },
  ];
  return (
    <div className="mt-2.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
      {chips.map((chip) => (
        <Badge key={chip.label} title={chip.label}>
          <span className="text-ink-subtle">{chip.label}</span>
          <span>{chip.value}</span>
        </Badge>
      ))}
    </div>
  );
}

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const isUser = message.role === "user";
  // An assistant turn stopped (via the Stop button, or a dropped connection)
  // before any token arrived has nothing to render in the text paragraph
  // below -- without this, that shows up as a near-invisible empty bubble.
  const stoppedWithNoText =
    !isUser && message.status === "done" && message.text === "" && !message.meta;

  return (
    <div className={cn("flex flex-col gap-1", isUser ? "items-end" : "items-start")}>
      <span className="px-1 text-xs font-medium text-ink-subtle">
        {isUser ? "You" : "Assistant"}
      </span>
      <div
        className={cn(
          "max-w-2xl rounded-card px-4 py-3 text-sm",
          isUser ? "bg-primary text-primary-ink" : "border border-line bg-surface text-ink",
        )}
      >
        {stoppedWithNoText ? (
          <p className="italic text-ink-subtle">Stopped before any response arrived.</p>
        ) : (
          <p className="whitespace-pre-wrap break-words">
            {message.text}
            {message.status === "streaming" && (
              <span aria-hidden className="ml-0.5 inline-block animate-pulse text-ink-subtle">
                ▍
              </span>
            )}
          </p>
        )}

        {message.status === "error" && message.error ? (
          <Alert tone="danger" className="mt-2.5">
            {message.error.message}
          </Alert>
        ) : null}

        {message.meta ? <MetaChips meta={message.meta} /> : null}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm run test -- src/components/chat/ChatMessage.test.tsx`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the gates and commit**

Run: `npm run test` (expected: **70 tests in 13 files**), `npm run typecheck`, `npm run lint`.

```bash
git add apps/web/src/components/chat/ChatMessage.tsx apps/web/src/components/chat/ChatMessage.test.tsx
git commit -m "$(cat <<'MSG'
feat(web): turn the chat turn's metadata into labelled chips

Model, tokens, cost and latency were a grey run-on sentence; each is now a
labelled chip. Turns get a role label, and an error renders as an Alert
while keeping the partial text the user already saw.

The exported types are unchanged, and the honest "not priced" label plus the
stopped-before-any-token placeholder now have tests, since both are easy to
lose in a rewrite and neither is obvious from the markup.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 14: The playground

**Files:**
- Modify: `apps/web/src/app/dashboard/playground/page.tsx` — the import block (lines 1–13) and the render (lines 188–302)

**Interfaces:**
- Consumes: `sessionTotals` (Task 6); `EmptyState`, `Badge`, `LoadingState`, `Icon` (Task 5); `Button`, `ButtonLink` (Task 3); `Select`/`Textarea` (Task 4, without `Field` — see the constraint on toolbar controls); `agentStatusTone`/`agentStatusLabel` (Task 6); `ChatMessage` (Task 13); the full-bleed `main` from Task 8. `PageHeader` is deliberately not used: the toolbar is this page's header, and a title bar above it would cost a row the transcript wants.
- Produces: nothing other tasks consume.

> **Hard constraint for this task.** Lines 20–186 — `newId`, every `useState`/`useMemo`/`useRef`, both `useEffect`s, `onSelectAgent`, `onNewConversation`, `finalizeStreamingMessage`, `sendMessage`, and `onStop` — **must not change by a single character**. This page's streaming and conversation-identity logic is the most carefully reasoned code in the app; this task is presentation only. If you find yourself editing inside `sendMessage`, stop: that is a different task and a different plan.

- [ ] **Step 1: Replace the import block**

```tsx
"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "urql";
import { ChatMessage, type ChatMessageData } from "@/components/chat/ChatMessage";
import { Badge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/icons";
import { Select, Textarea } from "@/components/ui/Input";
import { LoadingState } from "@/components/ui/Spinner";
import { AgentsDocument } from "@/graphql/generated";
import { agentStatusLabel, agentStatusTone } from "@/lib/agent-status";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { NEW_CONVERSATION, resolveTurnOutcome, type TurnState } from "@/lib/chat-turn";
import { sessionTotals } from "@/lib/chat-totals";
import { streamChat } from "@/lib/sse";
```

- [ ] **Step 2: Add the module constant and the derived values**

At module scope, below the imports and beside the existing `newId` helper:

```tsx
const EXAMPLE_PROMPTS = [
  "What do you sell, and who is it for?",
  "How much does the starter plan cost?",
  "Can you compare your two cheapest options?",
];
```

Then inside `PlaygroundContent`, immediately after `onStop`. These are reads of existing state; they add no behaviour.

```tsx
  const selectedAgent = agents.find((candidate) => String(candidate.id) === agentId);

  // The playground's job is to tell you what a real conversation costs. The
  // numbers were already arriving per turn and were never added up.
  const totals = sessionTotals(
    messages.flatMap((message) => (message.meta ? [message.meta] : [])),
  );
```

- [ ] **Step 3: Replace the three render branches (lines 188–293)**

```tsx
  if (loading || fetching) {
    return <LoadingState label="Loading the playground…" />;
  }

  if (agents.length === 0) {
    return (
      <div className="mx-auto w-full max-w-6xl px-6 py-8">
        <EmptyState
          icon="playground"
          title="No agents to test yet"
          description="The playground runs a real conversation against one of your agents, and streams back its answer with tokens, latency and cost."
          action={<ButtonLink href="/dashboard/agents">Create an agent</ButtonLink>}
        />
      </div>
    );
  }

  return (
    // Three rows in a full-height grid: the transcript is the only scroll
    // container, so the composer stays put without any viewport arithmetic.
    <section className="grid h-full grid-rows-[auto_1fr_auto]">
      <div className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-6 py-3">
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <span className="font-medium">Agent</span>
          <Select
            value={agentId ?? ""}
            onChange={(e) => onSelectAgent(e.target.value)}
            disabled={fetching || isStreaming}
            className="w-auto min-w-48"
          >
            {agents.map((agent) => (
              <option key={String(agent.id)} value={String(agent.id)}>
                {agent.name}
              </option>
            ))}
          </Select>
        </label>

        {selectedAgent ? (
          <>
            <Badge tone={agentStatusTone(selectedAgent.status)}>
              {agentStatusLabel(selectedAgent.status)}
            </Badge>
            <Badge>{selectedAgent.model}</Badge>
          </>
        ) : null}

        <div className="ml-auto flex items-center gap-3">
          {totals.pricedTurns + totals.unpricedTurns > 0 ? (
            <p className="text-xs text-ink-muted">
              <span className="font-medium text-ink">
                ${totals.costUsd.toFixed(4)}
              </span>{" "}
              · {totals.inputTokens} in / {totals.outputTokens} out
              {totals.unpricedTurns > 0 ? ` · ${totals.unpricedTurns} unpriced` : ""}
            </p>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            onClick={onNewConversation}
            disabled={isStreaming || messages.length === 0}
          >
            New conversation
          </Button>
        </div>
      </div>

      <div className="min-h-0 space-y-5 overflow-y-auto bg-surface-muted px-6 py-6">
        {messages.length === 0 ? (
          <EmptyState
            icon="playground"
            title="Ask your agent something"
            description="Its answer streams back token by token, with the model, cost and latency of the turn."
            action={
              <div className="flex flex-wrap justify-center gap-2">
                {EXAMPLE_PROMPTS.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    onClick={() => setInput(prompt)}
                    className="rounded-control border border-line bg-surface px-3 py-1.5 text-xs text-ink-muted hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            }
          />
        ) : (
          messages.map((message) => <ChatMessage key={message.id} message={message} />)
        )}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void sendMessage();
        }}
        className="border-t border-line bg-surface px-6 py-4"
      >
        <div className="flex items-end gap-3">
          <label className="flex-1">
            <span className="sr-only">Message</span>
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void sendMessage();
                }
              }}
              disabled={isStreaming || !agentId}
              rows={2}
              placeholder="Ask the agent something…"
            />
          </label>
          {isStreaming ? (
            <Button type="button" variant="danger" onClick={onStop}>
              <Icon name="stop" className="size-4" />
              Stop
            </Button>
          ) : (
            <Button type="submit" disabled={!input.trim() || !agentId}>
              <Icon name="send" className="size-4" />
              Send
            </Button>
          )}
        </div>
        <p className="mt-1.5 text-xs text-ink-subtle">
          Enter to send · Shift+Enter for a new line
        </p>
      </form>
    </section>
  );
```

- [ ] **Step 4: Update the Suspense fallback**

```tsx
export default function PlaygroundPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading the playground…" />}>
      <PlaygroundContent />
    </Suspense>
  );
}
```

- [ ] **Step 5: Confirm the logic is untouched**

Run: `git diff apps/web/src/app/dashboard/playground/page.tsx`
Expected: the diff touches only the import block, the block of derived values added after `onStop`, the render branches, and the Suspense fallback. **No hunk may fall inside `sendMessage`, `onSelectAgent`, `onNewConversation`, `finalizeStreamingMessage`, or either `useEffect`.** If one does, revert it.

- [ ] **Step 6: Run the gates**

Run: `npm run test` (expected: **70 tests in 13 files**, including the 22 unmodified baseline tests), `npm run typecheck`, `npm run lint`.

- [ ] **Step 7: Verify in the browser**

With `npm run dev` at `/dashboard/playground`:
1. The page **fills the window** — toolbar at the top, composer at the bottom, no gap under it and no page-level scrollbar.
2. With an empty transcript, three example prompts appear; clicking one fills the composer.
3. Send a message: tokens stream in, the caret blinks, and on completion the turn shows four labelled chips.
4. After one turn, the toolbar shows a running **cost and token total**. On the `fake` provider that cost is `$0.0000` and nothing is marked unpriced; point an agent at a model absent from the API's pricing table and the total gains `1 unpriced`.
5. Send a long message so the transcript overflows — **only the transcript scrolls**; the toolbar and composer stay fixed.
6. Press `Stop` mid-stream — the partial text is kept and the turn closes out.
7. Switch agents mid-session — the transcript clears and the totals reset.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/app/dashboard/playground/page.tsx
git commit -m "$(cat <<'MSG'
feat(web): give the playground the room it needs, and a session total

The page becomes a three-row grid inside the shell's full-bleed main, so the
transcript is the only scroll container and the h-[calc(100vh-4rem)]
max-h-[900px] guess -- which never matched the padding it compensated for --
is gone.

The toolbar now totals the session's tokens and cost, counting unpriced
turns separately rather than summing them as free, and an empty transcript
offers three example prompts so the page explains itself.

Streaming and conversation-identity logic is byte-for-byte unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 15: The placeholder pages

**Files:**
- Create: `apps/web/src/components/PlaceholderPage.tsx`
- Delete: `apps/web/src/components/ComingSoon.tsx`
- Rewrite: `apps/web/src/app/dashboard/knowledge/page.tsx`
- Rewrite: `apps/web/src/app/dashboard/products/page.tsx`
- Rewrite: `apps/web/src/app/dashboard/leads/page.tsx`
- Rewrite: `apps/web/src/app/dashboard/prompts/page.tsx`

**Interfaces:**
- Consumes: `PageHeader`, `Card`, `EmptyState`, `Badge`, `Icon` (Task 5); `IconName` (Task 5).
- Produces: `PlaceholderPage({ title, phase, icon, description, planned })` where `planned: string[]`.

- [ ] **Step 1: Write `PlaceholderPage.tsx`**

```tsx
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import type { IconName } from "@/components/ui/icons";
import { PageHeader } from "@/components/ui/PageHeader";

/**
 * A section the navigation shows before it exists. The old version said only
 * that it was "arriving in Phase N"; saying what will live here is the part
 * that makes the wait informative.
 */
export function PlaceholderPage({
  title,
  phase,
  icon,
  description,
  planned,
}: {
  title: string;
  phase: string;
  icon: IconName;
  description: string;
  planned: string[];
}) {
  return (
    <div className="space-y-4">
      <PageHeader title={title} description={description} meta={<Badge tone="warn">{phase}</Badge>} />
      <Card>
        <EmptyState
          icon={icon}
          title={`${title} arrives in ${phase}`}
          description="The navigation shows this section now so the shape of the product is visible while the backend catches up."
        />
        <div className="border-t border-line px-5 py-4">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
            What will live here
          </p>
          <ul className="mt-2 space-y-1.5 text-sm text-ink-muted">
            {planned.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Rewrite the four pages**

`knowledge/page.tsx`:

```tsx
import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function KnowledgePage() {
  return (
    <PlaceholderPage
      title="Knowledge"
      phase="Phase 3"
      icon="knowledge"
      description="The documents your assistant answers from."
      planned={[
        "Upload PDFs, docs and pasted text.",
        "Chunking and embedding, with indexing progress per document.",
        "Retrieval preview: see which chunks an answer drew on.",
      ]}
    />
  );
}
```

`products/page.tsx`:

```tsx
import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function ProductsPage() {
  return (
    <PlaceholderPage
      title="Products"
      phase="Phase 4"
      icon="product"
      description="The catalogue your assistant can quote from and recommend."
      planned={[
        "Products with prices, descriptions and availability.",
        "A catalogue lookup tool the agent can call mid-answer.",
        "CSV import, so the catalogue is not typed in twice.",
      ]}
    />
  );
}
```

`leads/page.tsx`:

```tsx
import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function LeadsPage() {
  return (
    <PlaceholderPage
      title="Leads"
      phase="Phase 4"
      icon="lead"
      description="The customers your assistant captured, and what they asked for."
      planned={[
        "Leads captured by the agent during a conversation.",
        "The conversation each lead came from.",
        "Export, and a webhook for your CRM.",
      ]}
    />
  );
}
```

`prompts/page.tsx`:

```tsx
import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function PromptsPage() {
  return (
    <PlaceholderPage
      title="Prompts"
      phase="Phase 2"
      icon="prompt"
      description="The versioned system prompts behind your agents."
      planned={[
        "Edit a prompt and publish a new version.",
        "Version history, with the ability to roll back.",
        "See which agents use which prompt version.",
      ]}
    />
  );
}
```

Note: prompts are already versioned data in the API (the backend work is done); what is missing is this UI. `Phase 2` is correct and matches the old `ComingSoon` label.

- [ ] **Step 3: Delete the old component**

```bash
git rm apps/web/src/components/ComingSoon.tsx
```

- [ ] **Step 4: Run the gates**

Run: `npm run test` (expected: **70 tests in 13 files**), `npm run typecheck`, `npm run lint`.
Expected: all clean. Typecheck is what proves nothing still imports `ComingSoon`.

- [ ] **Step 5: Verify in the browser**

Visit all four of `/dashboard/knowledge`, `/dashboard/products`, `/dashboard/leads`, `/dashboard/prompts`. Each shows its title with a phase badge, an icon, and a "What will live here" list. The sidebar keeps the correct item highlighted on each.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/PlaceholderPage.tsx apps/web/src/app/dashboard/knowledge/page.tsx apps/web/src/app/dashboard/products/page.tsx apps/web/src/app/dashboard/leads/page.tsx apps/web/src/app/dashboard/prompts/page.tsx
git commit -m "$(cat <<'MSG'
feat(web): tell the placeholder pages what they are waiting for

ComingSoon said only "arriving in Phase N". PlaceholderPage names the phase
in a badge and lists what will live in the section, so the four badged nav
items lead somewhere informative instead of somewhere empty.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 16: Enforce the conventions, then verify the whole thing

**Files:**
- Test: `apps/web/src/conventions.test.ts`
- Modify: `docs/superpowers/specs/2026-09-15-web-ui-foundation-design.md` (risk 1 only)

**Interfaces:**
- Consumes: the finished `src/` tree.
- Produces: a test that fails the build when a page reaches past the token layer.

- [ ] **Step 1: Write the guard test**

`apps/web/src/conventions.test.ts`. The spec's §7 says "a page naming `slate-*` is a review failure"; this is what makes that true without a reviewer having to notice.

```ts
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/** Raw Tailwind palette classes. The token layer in `globals.css` is the only
 * place a colour is named, so any of these in a component means someone
 * reached past it and the next theme change will miss them. */
const RAW_PALETTE =
  /\b(?:bg|text|border|ring|divide|from|via|to|placeholder|outline|decoration|shadow|fill|stroke|accent|caret)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b/g;

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !entry.name.endsWith(".test.ts") &&
      !entry.name.endsWith(".test.tsx")
      ? [path]
      : [];
  });
}

describe("token conventions", () => {
  it("names no raw Tailwind palette colour anywhere in src/", () => {
    const offenders = [...sourceFiles("src/app"), ...sourceFiles("src/components")].flatMap(
      (file) => {
        const matches = readFileSync(file, "utf8").match(RAW_PALETTE);
        return matches ? [`${file}: ${[...new Set(matches)].join(", ")}`] : [];
      },
    );
    expect(offenders).toEqual([]);
  });

  it("has no bare Loading… string outside the LoadingState primitive", () => {
    const offenders = [...sourceFiles("src/app"), ...sourceFiles("src/components")]
      .filter((file) => !file.endsWith(join("ui", "Spinner.tsx")))
      .filter((file) => readFileSync(file, "utf8").includes(">Loading"));
    expect(offenders).toEqual([]);
  });
});
```

- [ ] **Step 2: Run it**

Run: `npm run test -- src/conventions.test.ts`
Expected: PASS, 2 tests. **If it fails, it has found real work** — fix the named files to use tokens rather than relaxing the pattern.

- [ ] **Step 3: Close out risk 1 in the spec**

In `docs/superpowers/specs/2026-09-15-web-ui-foundation-design.md`, replace risk 1's body with:

```markdown
1. **Tailwind v4 `@theme` custom colour names — resolved.** Verified against
   Tailwind 4.3.3 in this project: `bg-canvas`, `text-ink-muted`,
   `border-line`, and `rounded-card` all compile to `var(--color-*)` /
   `var(--radius-*)` references. `apps/web/src/app/globals.test.ts` asserts the
   full token contract on every run, so the fallback is not needed.
```

- [ ] **Step 4: Full verification sweep**

Run each and record the actual output — do not claim any of them passed without seeing it:

```bash
cd apps/web
npm run test
npm run typecheck
npm run lint
npm run build
```

Expected:
- `npm run test` — **72 tests in 14 files**, including the **22 unmodified baseline tests** in `src/lib/sse.test.ts` and `src/lib/chat-turn.test.ts`.
- `npm run typecheck` — no output.
- `npm run lint` — no errors.
- `npm run build` — succeeds. This is the first task that runs it, and it is what catches a client/server component boundary mistake that `dev` tolerates.

- [ ] **Step 5: Confirm nothing outside the presentation layer moved**

```bash
cd /c/code/saas-ai
git diff --stat main -- apps/api packages
git diff main --name-only -- apps/web/src/lib/sse.ts apps/web/src/lib/chat-turn.ts apps/web/src/lib/auth.tsx apps/web/src/lib/urql.tsx apps/web/src/lib/api.ts apps/web/src/middleware.ts apps/web/src/graphql
```

Expected: **both produce no output.** Any file listed here is a constraint violation — revert it.

- [ ] **Step 6: Final walkthrough**

With `npm run dev`, walk the whole product once at desktop width and once below 1024px: register a workspace, land on Overview, work the checklist, create an agent, configure it, test it in the playground, visit each placeholder page, sign out. Note anything that looks wrong; fix it in this task rather than leaving it.

- [ ] **Step 7: Commit**

```bash
git add apps/web/src/conventions.test.ts docs/superpowers/specs/2026-09-15-web-ui-foundation-design.md
git commit -m "$(cat <<'MSG'
test(web): fail the build when a component reaches past the token layer

The spec says a page naming slate-* is a review failure; this makes that
true without a reviewer having to spot it, and does the same for bare
"Loading…" strings outside the LoadingState primitive.

Also closes risk 1 in the design doc: custom @theme colour names are
verified to generate utilities on Tailwind 4.3.3, so the var(--color-*)
fallback is not needed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Test count ledger

Each task's expected total, so a drifting count is caught at the task that caused it:

| After task | Test files | Tests | Added |
|---|---|---|---|
| baseline | 2 | 22 | — |
| 1 — test infrastructure | 3 | 24 | +2 |
| 2 — design tokens | 4 | 28 | +4 |
| 3 — Button | 5 | 32 | +4 |
| 4 — Field, Input | 6 | 37 | +5 |
| 5 — display primitives | 7 | 40 | +3 |
| 6 — pure logic layer | 11 | 57 | +17 |
| 7 — nav | 12 | 66 | +9 |
| 8 — shell | 12 | 66 | +0 |
| 9 — auth pages | 12 | 66 | +0 |
| 10 — Overview | 12 | 66 | +0 |
| 11 — agents list | 12 | 66 | +0 |
| 12 — agent detail | 12 | 66 | +0 |
| 13 — ChatMessage | 13 | 70 | +4 |
| 14 — playground | 13 | 70 | +0 |
| 15 — placeholder pages | 13 | 70 | +0 |
| 16 — conventions guard | 14 | 72 | +2 |

Tasks 8–12, 14 and 15 add no tests by design — see the last Global Constraint. They are verified by `typecheck`, `lint`, and their own visual checklists.

