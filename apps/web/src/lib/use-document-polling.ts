"use client";

import { useEffect } from "react";
import type { DocumentStatus } from "@/graphql/generated";
import { shouldPollDocuments } from "./document-status";

/** A handful of rows, checked every few seconds -- this is exactly the case
 * `docs/PHASE-3.md`'s honesty about "polling, not websockets" describes as
 * the plain solution rather than a shortcut. */
export const POLL_INTERVAL_MS = 3000;

/**
 * Runs `onPoll` every `POLL_INTERVAL_MS` while `shouldPollDocuments` says
 * to, and guarantees the interval is torn down whenever the effect re-runs
 * and on unmount.
 *
 * Split out from `KnowledgePage` specifically so the *mechanism* has a test,
 * not just the predicate: `shouldPollDocuments` being correct does not by
 * itself prove the interval it gates is ever cleared. A version of this
 * effect that dropped its `return () => clearInterval(interval)` passed
 * every other test in this codebase (typecheck, lint, all 173 tests) while
 * leaking a timer that hammers the API forever from a backgrounded tab --
 * see `use-document-polling.test.ts`, which fails red against exactly that
 * mutation.
 */
export function useDocumentPolling(
  documents: readonly { status: DocumentStatus }[],
  tabHidden: boolean,
  onPoll: () => void,
): void {
  useEffect(() => {
    if (!shouldPollDocuments(documents, tabHidden)) return;
    const interval = setInterval(onPoll, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [documents, tabHidden, onPoll]);
}
