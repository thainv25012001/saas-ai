/**
 * Puts the cursor back in the message box when a turn ends.
 *
 * The composer is `disabled` while an answer streams, and the browser blurs a
 * disabled element without ever restoring focus when it is re-enabled. So
 * sending a message costs you the cursor for the whole turn and does not give
 * it back -- you have to click the box again before typing the next question.
 */

import { useEffect, useRef } from "react";

/**
 * Whether the composer may take focus back.
 *
 * Only when nothing else holds it. Focus sitting on `body` (or nowhere) is
 * exactly the state the disable above leaves behind; anything else is where
 * the user deliberately put it -- the model picker, say, which stays usable
 * mid-stream -- and a turn finishing must not yank the cursor out of it.
 */
export function shouldRestoreFocus(active: Element | null, body: Element | null): boolean {
  return active === null || active === body;
}

/** Attach the returned ref to the composer. */
export function useComposerFocus(isStreaming: boolean) {
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (isStreaming) return;
    if (!shouldRestoreFocus(document.activeElement, document.body)) return;
    // Also runs on mount, which is deliberate: arriving at the playground,
    // the one thing you are there to do is type a question.
    ref.current?.focus();
  }, [isStreaming]);

  return ref;
}
