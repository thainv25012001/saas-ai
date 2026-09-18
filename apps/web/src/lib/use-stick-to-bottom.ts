/**
 * Keeps a scroll container pinned to its newest content -- but only while the
 * reader is actually at the bottom.
 *
 * The unconditional version of this ("scroll to the end whenever anything
 * changes") is worse than no auto-scroll at all on a streaming transcript: a
 * token arrives every few milliseconds, so scrolling up to re-read an earlier
 * answer yanks the view back down before you can finish the sentence. So the
 * hook tracks whether the reader is at the end and follows only then, which is
 * what `isAtBottom` below decides.
 */

import { useCallback, useEffect, useRef } from "react";

/**
 * How far from the end still counts as "at the end".
 *
 * Not zero: on a fractional device pixel ratio the browser leaves
 * `scrollHeight - scrollTop - clientHeight` at a fraction rather than 0 even
 * when the container is scrolled fully down, and a strict check would then
 * decide the reader had scrolled up and stop following. It is also the small
 * grace margin that keeps following after a nudge of the wheel.
 */
export const BOTTOM_TOLERANCE_PX = 32;

/** The three layout properties this module reads off the container. Named as
 * its own type so the hook can be driven in a test without a real layout
 * engine -- see `use-stick-to-bottom.test.ts`. */
export type ScrollMetrics = {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
};

export function isAtBottom(el: ScrollMetrics, tolerance = BOTTOM_TOLERANCE_PX): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= tolerance;
}

/**
 * `dep` is whatever changes when the content does -- for the playground, the
 * messages array, whose identity changes on every streamed token.
 *
 * Returns the `ref` and `onScroll` the container must be given, plus `stick()`
 * for the one case no scroll event covers: content replaced wholesale (a new
 * conversation, a different agent). Nothing scrolls then, so a reader who had
 * scrolled up in the old transcript would still be marked as not-following in
 * the new one, and their first message would not scroll.
 */
export function useStickToBottom(dep: unknown) {
  const ref = useRef<HTMLDivElement | null>(null);
  // A ref, not state: this is read inside the effect below and must never
  // itself cause a render. Starts true so the first message scrolls.
  const followingRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (el) followingRef.current = isAtBottom(el);
  }, []);

  const stick = useCallback(() => {
    followingRef.current = true;
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (el && followingRef.current) el.scrollTop = el.scrollHeight;
  }, [dep]);

  return { ref, onScroll, stick };
}
