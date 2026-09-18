// @vitest-environment happy-dom
import { renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { readStoredFlag, useRememberedFlag, writeStoredFlag } from "./use-remembered-flag";

const KEY = "test:flag";

afterEach(() => {
  // Un-stub FIRST: one test replaces `localStorage` with an object that
  // throws and has no `clear`, and clearing before restoring would fail the
  // teardown and leak that stub into every test after it.
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("readStoredFlag", () => {
  it("round-trips a written value", () => {
    writeStoredFlag(KEY, true);
    expect(readStoredFlag(KEY)).toBe(true);
    writeStoredFlag(KEY, false);
    expect(readStoredFlag(KEY)).toBe(false);
  });

  it("is null when nothing has been stored", () => {
    // Null, not false: "never chosen" has to be distinguishable from "chose
    // expanded", or a viewport default could never apply.
    expect(readStoredFlag(KEY)).toBeNull();
  });

  it("is null for a value that is not a stored flag", () => {
    window.localStorage.setItem(KEY, "banana");
    expect(readStoredFlag(KEY)).toBeNull();
  });

  it("survives storage being unavailable", () => {
    // A private window, blocked site data, or storage disabled: reading
    // throws rather than returning null, and an unguarded read takes the
    // whole page down.
    vi.stubGlobal("localStorage", {
      getItem() {
        throw new Error("access denied");
      },
      setItem() {
        throw new Error("access denied");
      },
    });

    expect(readStoredFlag(KEY)).toBeNull();
    expect(() => writeStoredFlag(KEY, true)).not.toThrow();
  });
});

describe("useRememberedFlag", () => {
  it("renders the server-safe initial value first, then applies what was stored", () => {
    // The first client render must match what the server produced, so the
    // stored value can only be applied after mount -- reading storage during
    // render is a hydration mismatch.
    writeStoredFlag(KEY, true);

    const { result } = renderHook(() => useRememberedFlag(KEY, () => false));

    expect(result.current[0]).toBe(true);
  });

  it("falls back to the computed default when nothing was stored", () => {
    const { result } = renderHook(() => useRememberedFlag(KEY, () => true));

    expect(result.current[0]).toBe(true);
  });

  it("prefers a stored choice over the computed default", () => {
    // Someone who expanded the panel on a narrow screen has said what they
    // want; the viewport must not keep overruling them.
    writeStoredFlag(KEY, false);

    const { result } = renderHook(() => useRememberedFlag(KEY, () => true));

    expect(result.current[0]).toBe(false);
  });

  it("persists a new value", () => {
    const { result } = renderHook(() => useRememberedFlag(KEY, () => false));

    act(() => result.current[1](true));

    expect(result.current[0]).toBe(true);
    expect(readStoredFlag(KEY)).toBe(true);
  });
});
