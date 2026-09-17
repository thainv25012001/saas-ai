import { describe, expect, it } from "vitest";
import {
  canRetryDocument,
  documentStatusLabel,
  documentStatusTone,
  isTerminalDocumentStatus,
  shouldPollDocuments,
} from "./document-status";

describe("isTerminalDocumentStatus", () => {
  it("treats ready and failed as terminal", () => {
    expect(isTerminalDocumentStatus("READY")).toBe(true);
    expect(isTerminalDocumentStatus("FAILED")).toBe(true);
  });

  it("treats pending and processing as non-terminal", () => {
    expect(isTerminalDocumentStatus("PENDING")).toBe(false);
    expect(isTerminalDocumentStatus("PROCESSING")).toBe(false);
  });
});

describe("canRetryDocument", () => {
  it("allows retry for failed and pending, matching the API's own check", () => {
    expect(canRetryDocument("FAILED")).toBe(true);
    expect(canRetryDocument("PENDING")).toBe(true);
  });

  it("rejects retry for processing and ready, which the API answers with a 409", () => {
    expect(canRetryDocument("PROCESSING")).toBe(false);
    expect(canRetryDocument("READY")).toBe(false);
  });
});

describe("documentStatusLabel / documentStatusTone", () => {
  it("labels a ready document with chunks as a plain success", () => {
    expect(documentStatusLabel("READY", 12)).toBe("Ready");
    expect(documentStatusTone("READY", 12)).toBe("success");
  });

  it("calls out a ready document with zero chunks instead of showing plain success", () => {
    // The backend calls this `ready` because it genuinely processed the
    // file; from the user's side a green badge with nothing behind it is
    // baffling, so the badge itself must carry the distinction.
    expect(documentStatusLabel("READY", 0)).toBe("Ready — no content extracted");
    expect(documentStatusTone("READY", 0)).toBe("warn");
  });

  it("marks failed as a danger tone, not the same warn tone as an empty ready", () => {
    expect(documentStatusTone("FAILED", 0)).toBe("danger");
  });

  it("labels pending and processing plainly", () => {
    expect(documentStatusLabel("PENDING", 0)).toBe("Pending");
    expect(documentStatusLabel("PROCESSING", 0)).toBe("Processing");
  });
});

describe("shouldPollDocuments", () => {
  it("polls while any document is still pending or processing", () => {
    expect(shouldPollDocuments([{ status: "READY" }, { status: "PENDING" }], false)).toBe(true);
  });

  it("stops once every document has reached a terminal status", () => {
    expect(shouldPollDocuments([{ status: "READY" }, { status: "FAILED" }], false)).toBe(false);
  });

  it("stops while the tab is hidden even if work is still in flight", () => {
    expect(shouldPollDocuments([{ status: "PENDING" }], true)).toBe(false);
  });

  it("does not poll an empty list", () => {
    expect(shouldPollDocuments([], false)).toBe(false);
  });

  it("restarts once an accepted retry puts a failed row back to pending", () => {
    // The T5 -> T8 seam. `retryDocument` used to leave the row on `FAILED`,
    // which this predicate calls terminal -- so the timer never started,
    // and the UI never learned the worker had finished. The API now moves
    // the row to `PENDING` when it accepts the retry
    // (`retry_document` in apps/api/app/api/documents.py), and this is the
    // half of that fix that lives here: `PENDING` has to put the list back
    // into the polling set.
    const settled = [{ status: "READY" as const }, { status: "FAILED" as const }];
    expect(shouldPollDocuments(settled, false)).toBe(false);

    const afterRetry = [{ status: "READY" as const }, { status: "PENDING" as const }];
    expect(shouldPollDocuments(afterRetry, false)).toBe(true);
  });
});
