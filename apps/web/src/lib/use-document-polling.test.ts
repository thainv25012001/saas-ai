// @vitest-environment happy-dom
import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentStatus } from "@/graphql/generated";
import { POLL_INTERVAL_MS, useDocumentPolling } from "./use-document-polling";

const PENDING: { status: DocumentStatus }[] = [{ status: "PENDING" }];
const TERMINAL: { status: DocumentStatus }[] = [{ status: "READY" }];

describe("useDocumentPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls onPoll on every tick while polling is due", () => {
    const onPoll = vi.fn();
    renderHook(({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll), {
      initialProps: { documents: PENDING, tabHidden: false },
    });

    vi.advanceTimersByTime(POLL_INTERVAL_MS * 3);

    expect(onPoll).toHaveBeenCalledTimes(3);
  });

  it("clears the interval on unmount, so a leaked timer cannot keep hammering the API", () => {
    // This is the mutation the brief warns about: a version of the effect
    // that drops its `return () => clearInterval(interval)` passes every
    // other test in this codebase (typecheck, lint, all other tests) while
    // leaking a timer forever. Deleting that cleanup line is what must turn
    // this test red.
    const onPoll = vi.fn();
    const { unmount } = renderHook(
      ({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll),
      { initialProps: { documents: PENDING, tabHidden: false } },
    );

    vi.advanceTimersByTime(POLL_INTERVAL_MS);
    expect(onPoll).toHaveBeenCalledTimes(1);

    unmount();
    vi.advanceTimersByTime(POLL_INTERVAL_MS * 5);

    // No further calls: the interval that fired once before unmount must
    // not still be running afterwards.
    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a rerender shows every document has reached a terminal status", () => {
    const onPoll = vi.fn();
    const { rerender } = renderHook(
      ({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll),
      { initialProps: { documents: PENDING, tabHidden: false } },
    );

    vi.advanceTimersByTime(POLL_INTERVAL_MS);
    expect(onPoll).toHaveBeenCalledTimes(1);

    rerender({ documents: TERMINAL, tabHidden: false });
    vi.advanceTimersByTime(POLL_INTERVAL_MS * 5);

    // The old interval must have been torn down when the effect re-ran,
    // not merely left to fire alongside a decision to stop scheduling a
    // new one.
    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a rerender shows the tab has gone into the background", () => {
    const onPoll = vi.fn();
    const { rerender } = renderHook(
      ({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll),
      { initialProps: { documents: PENDING, tabHidden: false } },
    );

    vi.advanceTimersByTime(POLL_INTERVAL_MS);
    expect(onPoll).toHaveBeenCalledTimes(1);

    rerender({ documents: PENDING, tabHidden: true });
    vi.advanceTimersByTime(POLL_INTERVAL_MS * 5);

    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it("resumes polling if the tab becomes visible again while work is still in flight", () => {
    const onPoll = vi.fn();
    const { rerender } = renderHook(
      ({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll),
      { initialProps: { documents: PENDING, tabHidden: true } },
    );

    vi.advanceTimersByTime(POLL_INTERVAL_MS * 3);
    expect(onPoll).not.toHaveBeenCalled();

    rerender({ documents: PENDING, tabHidden: false });
    vi.advanceTimersByTime(POLL_INTERVAL_MS);

    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it("never polls at all when nothing is pending or processing", () => {
    const onPoll = vi.fn();
    renderHook(({ documents, tabHidden }) => useDocumentPolling(documents, tabHidden, onPoll), {
      initialProps: { documents: TERMINAL, tabHidden: false },
    });

    vi.advanceTimersByTime(POLL_INTERVAL_MS * 5);

    expect(onPoll).not.toHaveBeenCalled();
  });
});
