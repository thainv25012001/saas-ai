import { describe, expect, it } from "vitest";
import { formatPrice } from "./format";

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
