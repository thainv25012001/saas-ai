import { describe, expect, it } from "vitest";
import { formatLatency, formatPrice, formatUsd } from "./format";

describe("formatPrice", () => {
  it("returns null for no price, so the caller chooses the empty-cell wording", () => {
    expect(formatPrice(null, "USD")).toBeNull();
  });

  it("formats a decimal string with its currency", () => {
    expect(formatPrice("32999.00", "USD")).toMatch(/32,999\.00/);
  });

  it("falls back to the amount and code as they arrived for an unknown currency", () => {
    // `Intl.NumberFormat` throws a RangeError for a malformed code; a price
    // must not disappear behind that.
    expect(formatPrice("10.00", "??X")).toBe("10.00 ??X");
  });

  it("formats an amount with no currency as a plain number", () => {
    expect(formatPrice("5", null)).toMatch(/5\.00/);
  });
});

describe("formatUsd", () => {
  it("shows a dash for an unpriced cost, never $0.00", () => {
    expect(formatUsd(null)).toBe("—");
  });

  it("keeps sub-cent precision", () => {
    expect(formatUsd("0.0412")).toMatch(/0\.0412/);
    expect(formatUsd("3")).toMatch(/3\.00/);
  });
});

describe("formatLatency", () => {
  it("uses ms under a second and seconds above", () => {
    expect(formatLatency(840)).toBe("840 ms");
    expect(formatLatency(2140)).toBe("2.1 s");
    expect(formatLatency(null)).toBe("—");
  });
});
