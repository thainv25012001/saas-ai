// @vitest-environment happy-dom
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  BOTTOM_TOLERANCE_PX,
  isAtBottom,
  type ScrollMetrics,
  useStickToBottom,
} from "./use-stick-to-bottom";

/**
 * A stand-in for the transcript's scroll container.
 *
 * Not a real element: happy-dom has no layout engine, so `scrollHeight` and
 * `clientHeight` on a real `<div>` are both 0 -- which makes every position
 * "the bottom" and would let a hook that never scrolls anything pass. Driving
 * the hook through an object with the metrics spelled out is what keeps these
 * tests able to tell "followed the content" from "did nothing".
 */
function container(metrics: ScrollMetrics): ScrollMetrics {
  return metrics;
}

/** The cast lives here, once: the hook only ever reads the three metrics above
 * and writes `scrollTop`, so a plain object satisfies it -- but the ref is
 * typed for the real `<div>` it holds in the page. */
function attach(ref: { current: HTMLDivElement | null }, el: ScrollMetrics): void {
  ref.current = el as unknown as HTMLDivElement;
}

describe("isAtBottom", () => {
  it("is true when the viewport sits exactly at the end of the content", () => {
    expect(isAtBottom({ scrollTop: 400, scrollHeight: 1000, clientHeight: 600 })).toBe(true);
  });

  it("is true within the tolerance, so a fractional scroll position still counts as the bottom", () => {
    // Sub-pixel device ratios leave `scrollHeight - scrollTop - clientHeight`
    // at a fraction rather than 0 even when the user is pinned to the end.
    expect(
      isAtBottom({ scrollTop: 400 - BOTTOM_TOLERANCE_PX, scrollHeight: 1000, clientHeight: 600 }),
    ).toBe(true);
  });

  it("is false once the user has scrolled up past the tolerance", () => {
    expect(
      isAtBottom({
        scrollTop: 400 - BOTTOM_TOLERANCE_PX - 1,
        scrollHeight: 1000,
        clientHeight: 600,
      }),
    ).toBe(false);
  });
});

describe("useStickToBottom", () => {
  it("scrolls to the end when the content grows", () => {
    // The reported bug: deleting the effect that writes `scrollTop` leaves
    // every other test in this file passing while new messages pile up below
    // the fold. This is the test that must go red for that.
    const el = container({ scrollTop: 0, scrollHeight: 600, clientHeight: 600 });
    const { result, rerender } = renderHook(({ dep }) => useStickToBottom(dep), {
      initialProps: { dep: 0 },
    });
    attach(result.current.ref, el);

    el.scrollHeight = 1000;
    rerender({ dep: 1 });

    expect(el.scrollTop).toBe(1000);
  });

  it("leaves the scroll position alone while the user is reading further up", () => {
    const el = container({ scrollTop: 0, scrollHeight: 1000, clientHeight: 600 });
    const { result, rerender } = renderHook(({ dep }) => useStickToBottom(dep), {
      initialProps: { dep: 0 },
    });
    attach(result.current.ref, el);

    // The user scrolls up, and the hook hears about it the way the real
    // container does -- through the scroll handler it hands the element.
    result.current.onScroll();

    el.scrollHeight = 2000;
    rerender({ dep: 1 });

    expect(el.scrollTop).toBe(0);
  });

  it("follows again once the user scrolls back down to the end", () => {
    const el = container({ scrollTop: 0, scrollHeight: 1000, clientHeight: 600 });
    const { result, rerender } = renderHook(({ dep }) => useStickToBottom(dep), {
      initialProps: { dep: 0 },
    });
    attach(result.current.ref, el);

    result.current.onScroll();
    el.scrollTop = 400;
    result.current.onScroll();

    el.scrollHeight = 2000;
    rerender({ dep: 1 });

    expect(el.scrollTop).toBe(2000);
  });

  it("follows again after stick(), for a transcript cleared without a scroll event", () => {
    // Switching agents or starting a new conversation empties the transcript.
    // No scroll event fires for that, so a user who had scrolled up stays
    // marked as "not following" and their next message would not scroll --
    // in a transcript that now holds only that message.
    const el = container({ scrollTop: 0, scrollHeight: 1000, clientHeight: 600 });
    const { result, rerender } = renderHook(({ dep }) => useStickToBottom(dep), {
      initialProps: { dep: 0 },
    });
    attach(result.current.ref, el);

    result.current.onScroll();
    result.current.stick();

    el.scrollHeight = 2000;
    rerender({ dep: 1 });

    expect(el.scrollTop).toBe(2000);
  });
});
