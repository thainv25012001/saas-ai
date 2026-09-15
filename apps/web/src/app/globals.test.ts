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
