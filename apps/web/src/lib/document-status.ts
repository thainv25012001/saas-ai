import type { BadgeTone } from "@/components/ui/Badge";
import type { DocumentStatus } from "@/graphql/generated";

/**
 * `pending`/`processing` are the only statuses a worker can still move on
 * from; `ready`/`failed` are where a document settles. Used to decide
 * whether the list still needs to poll (see `shouldPollDocuments`).
 */
export function isTerminalDocumentStatus(status: DocumentStatus): boolean {
  return status === "READY" || status === "FAILED";
}

/**
 * Mirrors `retry_document`'s own check in `apps/api/app/api/documents.py`:
 * `failed` obviously may be retried, and `pending` may too, because that
 * status only means "the row was committed", not "a job is guaranteed to
 * exist" -- the enqueue call happens after the row's own commit and can
 * fail on its own (Redis down, a crash in between). `processing` and
 * `ready` are rejected there with a 409, so the button is hidden for both
 * rather than offered and then failing.
 */
export function canRetryDocument(status: DocumentStatus): boolean {
  return status === "FAILED" || status === "PENDING";
}

/**
 * A `ready` document with zero chunks genuinely finished processing --
 * extraction found nothing chunkable (a blank file, a scan with no text
 * layer) -- so `failed` would be dishonest. But a green "Ready" badge next
 * to an assistant that never cites it is not self-explanatory either. This
 * is the one place that distinction is made, so every badge/label call site
 * shows it the same way instead of each guessing independently.
 */
function isEmptyReady(status: DocumentStatus, chunkCount: number): boolean {
  return status === "READY" && chunkCount === 0;
}

export function documentStatusLabel(status: DocumentStatus, chunkCount: number): string {
  switch (status) {
    case "PENDING":
      return "Pending";
    case "PROCESSING":
      return "Processing";
    case "FAILED":
      return "Failed";
    case "READY":
      return isEmptyReady(status, chunkCount) ? "Ready — no content extracted" : "Ready";
  }
}

export function documentStatusTone(status: DocumentStatus, chunkCount: number): BadgeTone {
  switch (status) {
    case "PENDING":
      return "neutral";
    case "PROCESSING":
      return "info";
    case "FAILED":
      return "danger";
    case "READY":
      return isEmptyReady(status, chunkCount) ? "warn" : "success";
  }
}

/**
 * Whether the list still needs its poll timer running. `false` while the
 * tab is hidden (there is no one to show a live update to, and a
 * backgrounded tab polling every few seconds is exactly the kind of thing
 * that adds up across many open tabs for no one's benefit) and `false` once
 * every row has settled into a terminal status -- there is nothing left
 * that could still change.
 */
export function shouldPollDocuments(
  documents: readonly { status: DocumentStatus }[],
  tabHidden: boolean,
): boolean {
  if (tabHidden) return false;
  return documents.some((document) => !isTerminalDocumentStatus(document.status));
}
