"use client";

import { useEffect, useState } from "react";

/** Tracks page visibility so a poll can stop while no one is looking at the
 * tab -- see `shouldPollDocuments` and `shouldPollImports`. Its own hook so a
 * poll effect only has to reason about one boolean, not the listener wiring. */
export function useTabHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  useEffect(() => {
    function onVisibilityChange() {
      setHidden(document.hidden);
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);
  return hidden;
}
