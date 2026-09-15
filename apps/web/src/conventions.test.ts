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
    const offenders = [
      ...sourceFiles("src/app"),
      ...sourceFiles("src/components"),
      ...sourceFiles("src/lib"),
    ].flatMap((file) => {
      const matches = readFileSync(file, "utf8").match(RAW_PALETTE);
      return matches ? [`${file}: ${[...new Set(matches)].join(", ")}`] : [];
    });
    expect(offenders).toEqual([]);
  });

  it("has no bare Loading… string outside the LoadingState primitive", () => {
    const offenders = [
      ...sourceFiles("src/app"),
      ...sourceFiles("src/components"),
      ...sourceFiles("src/lib"),
    ]
      .filter((file) => !file.endsWith(join("ui", "Spinner.tsx")))
      .filter((file) => readFileSync(file, "utf8").includes(">Loading"));
    expect(offenders).toEqual([]);
  });
});
