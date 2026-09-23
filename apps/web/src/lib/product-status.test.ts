import { describe, expect, it } from "vitest";
import {
  availabilityTone,
  importStatusLabel,
  importStatusTone,
  searchIndexLabel,
  shouldPollImports,
} from "./product-status";

describe("importStatusLabel / importStatusTone", () => {
  it("does not call a completed import with failed rows a clean success", () => {
    expect(importStatusLabel("COMPLETED", 0)).toBe("Completed");
    expect(importStatusTone("COMPLETED", 0)).toBe("success");
    expect(importStatusLabel("COMPLETED", 1)).toBe("Completed — 1 row failed");
    expect(importStatusTone("COMPLETED", 4)).toBe("warn");
  });

  it("reads a whole-file failure as failed", () => {
    expect(importStatusTone("FAILED", 0)).toBe("danger");
  });
});

describe("shouldPollImports", () => {
  it("polls while an import is queued or running", () => {
    expect(shouldPollImports([{ status: "COMPLETED" }, { status: "PENDING" }], false)).toBe(true);
    expect(shouldPollImports([{ status: "PROCESSING" }], false)).toBe(true);
  });

  it("stops once every import has settled", () => {
    expect(shouldPollImports([{ status: "COMPLETED" }, { status: "FAILED" }], false)).toBe(false);
  });

  it("never polls from a hidden tab", () => {
    expect(shouldPollImports([{ status: "PENDING" }], true)).toBe(false);
  });
});

describe("searchIndexLabel", () => {
  it("gives the normal, indexed state no label at all", () => {
    expect(searchIndexLabel("INDEXED")).toBeNull();
    expect(searchIndexLabel("NOT_INDEXED")).not.toBeNull();
    expect(searchIndexLabel("STALE")).not.toBeNull();
  });
});

describe("availabilityTone", () => {
  it("keeps in-stock and out-of-stock visibly different", () => {
    expect(availabilityTone("IN_STOCK")).not.toBe(availabilityTone("OUT_OF_STOCK"));
  });
});
