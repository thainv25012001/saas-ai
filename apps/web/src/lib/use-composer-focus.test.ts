// @vitest-environment happy-dom
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { shouldRestoreFocus, useComposerFocus } from "./use-composer-focus";

afterEach(() => {
  document.body.innerHTML = "";
});

/** A composer attached to the real document -- `focus()` is a no-op on a
 * detached element, so a test that skipped this would pass either way. */
function mountComposer(): HTMLTextAreaElement {
  const textarea = document.createElement("textarea");
  document.body.append(textarea);
  return textarea;
}

describe("shouldRestoreFocus", () => {
  it("restores when nothing holds focus", () => {
    expect(shouldRestoreFocus(document.body, document.body)).toBe(true);
    expect(shouldRestoreFocus(null, document.body)).toBe(true);
  });

  it("leaves focus where the user put it", () => {
    // Disabling the composer during a stream is what drops focus to the body.
    // Anything else holding it is the user's own doing -- the model picker,
    // say -- and a turn ending must not yank the cursor out of it.
    const picker = document.createElement("select");
    document.body.append(picker);
    expect(shouldRestoreFocus(picker, document.body)).toBe(false);
  });
});

describe("useComposerFocus", () => {
  it("puts the cursor back in the composer when the turn ends", () => {
    const textarea = mountComposer();
    const { result, rerender } = renderHook(({ streaming }) => useComposerFocus(streaming), {
      initialProps: { streaming: true },
    });
    result.current.current = textarea;

    rerender({ streaming: false });

    expect(document.activeElement).toBe(textarea);
  });

  it("does not grab focus while the answer is still streaming", () => {
    const textarea = mountComposer();
    const { result, rerender } = renderHook(({ streaming }) => useComposerFocus(streaming), {
      initialProps: { streaming: false },
    });
    result.current.current = textarea;

    rerender({ streaming: true });

    expect(document.activeElement).not.toBe(textarea);
  });

  it("does not steal focus from a control the user is using", () => {
    const textarea = mountComposer();
    const picker = document.createElement("select");
    document.body.append(picker);
    const { result, rerender } = renderHook(({ streaming }) => useComposerFocus(streaming), {
      initialProps: { streaming: true },
    });
    result.current.current = textarea;
    picker.focus();

    rerender({ streaming: false });

    expect(document.activeElement).toBe(picker);
  });
});
