import { describe, expect, it } from "vitest";
import { readableTextOn, relativeLuminance } from "./contrast";

describe("relativeLuminance", () => {
  it("is 0 for black and 1 for white", () => {
    expect(relativeLuminance("#000000")).toBe(0);
    expect(relativeLuminance("#ffffff")).toBeCloseTo(1, 10);
  });

  it("weights green far above blue (WCAG coefficients)", () => {
    expect(relativeLuminance("#00ff00")).toBeCloseTo(0.7152, 4);
    expect(relativeLuminance("#0000ff")).toBeCloseTo(0.0722, 4);
  });
});

describe("readableTextOn", () => {
  it.each([
    ["#ffffff", "#000000"],
    ["#fef08a", "#000000"], // pale yellow
    ["#a7f3d0", "#000000"], // pale mint
    ["#00ff00", "#000000"],
    ["#000000", "#ffffff"],
    ["#2563eb", "#ffffff"], // the default brand blue
    ["#0f766e", "#ffffff"],
    ["#7c3aed", "#ffffff"],
  ])("picks the more contrasting text for %s", (background, expected) => {
    expect(readableTextOn(background)).toBe(expected);
  });

  it("accepts upper-case hex", () => {
    expect(readableTextOn("#FEF08A")).toBe("#000000");
  });

  it("falls back to white for anything that is not a 6-digit hex", () => {
    expect(readableTextOn("red")).toBe("#ffffff");
    expect(readableTextOn("#fff")).toBe("#ffffff");
  });
});
