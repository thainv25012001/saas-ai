import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_UPLOAD_BYTES } from "./documents";
import {
  ACCEPTED_IMPORT_EXTENSIONS,
  importProducts,
  MAX_IMPORT_BYTES,
  resolveImportType,
  validateImportFile,
} from "./products";

describe("validateImportFile", () => {
  it("accepts a CSV and a JSON catalogue under the limit", () => {
    expect(validateImportFile({ type: "text/csv", size: 1024 })).toBeNull();
    expect(validateImportFile({ type: "application/json", size: 1024 })).toBeNull();
  });

  it("rejects an unsupported type and names what is accepted", () => {
    const error = validateImportFile({ type: "application/vnd.ms-excel", size: 1024, name: "a.xls" });
    expect(error?.code).toBe("unsupported_import_type");
    for (const ext of ACCEPTED_IMPORT_EXTENSIONS) {
      expect(error?.message).toContain(ext);
    }
  });

  it("rejects a file over the shared request budget", () => {
    expect(MAX_IMPORT_BYTES).toBe(MAX_UPLOAD_BYTES);
    const error = validateImportFile({ type: "text/csv", size: MAX_IMPORT_BYTES + 1 });
    expect(error?.code).toBe("payload_too_large");
  });

  it("falls back to the extension when the browser reports no useful type", () => {
    // `.csv` on a machine with no spreadsheet app installed reports "" or
    // octet-stream -- the server resolves it the same way.
    expect(resolveImportType("", "catalogue.CSV")).toBe("text/csv");
    expect(resolveImportType("application/octet-stream", "catalogue.json")).toBe("application/json");
    expect(validateImportFile({ type: "", size: 10, name: "catalogue.csv" })).toBeNull();
  });

  it("believes a reported type even when the extension disagrees", () => {
    expect(resolveImportType("text/plain", "catalogue.csv")).toBe("text/plain");
  });
});

describe("importProducts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the file as multipart form data with a Bearer token and no manual Content-Type", async () => {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify({ id: "i1", status: "pending" }), { status: 202 });
    });
    const file = new File(["external_id,name\n"], "catalogue.csv", { type: "text/csv" });

    await importProducts(file, { accessToken: "tok", apiUrl: "http://api.test" });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://api.test/api/v1/products/import");
    expect(calls[0].init?.method).toBe("POST");
    expect((calls[0].init?.body as FormData).get("file")).toBeInstanceOf(File);
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok");
    expect(headers["Content-Type"]).toBeUndefined();
  });

  it("throws the server's error envelope on a rejection", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify({ error: { code: "unsupported_import_type", message: "nope" } }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const file = new File(["x"], "a.xml", { type: "application/xml" });

    await expect(importProducts(file, { accessToken: "tok", apiUrl: "http://api.test" })).rejects.toEqual({
      code: "unsupported_import_type",
      message: "nope",
    });
  });
});
