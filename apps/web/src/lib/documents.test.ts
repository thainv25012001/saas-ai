import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ACCEPTED_DOCUMENT_EXTENSIONS,
  formatBytes,
  MAX_UPLOAD_BYTES,
  retryDocument,
  uploadDocument,
  validateDocumentFile,
} from "./documents";

describe("formatBytes", () => {
  it("renders whole megabytes without a decimal", () => {
    expect(formatBytes(20 * 1024 * 1024)).toBe("20 MB");
  });

  it("keeps a decimal below ten units, where it is the difference that matters", () => {
    expect(formatBytes(1.5 * 1024)).toBe("1.5 KB");
  });

  it("renders sub-kilobyte sizes in bytes", () => {
    expect(formatBytes(512)).toBe("512 B");
  });
});

describe("validateDocumentFile", () => {
  it("accepts a supported type under the size limit", () => {
    expect(validateDocumentFile({ type: "application/pdf", size: 1024 })).toBeNull();
  });

  it("rejects an unsupported type before any upload is attempted", () => {
    const error = validateDocumentFile({ type: "application/zip", size: 1024 });
    expect(error?.code).toBe("unsupported_document_type");
    // The message names what IS accepted -- requirement 4 is failing loud
    // and early, not failing silently.
    for (const ext of ACCEPTED_DOCUMENT_EXTENSIONS) {
      expect(error?.message).toContain(ext);
    }
  });

  it("rejects a file over the usable upload budget", () => {
    const error = validateDocumentFile({ type: "text/plain", size: MAX_UPLOAD_BYTES + 1 });
    expect(error?.code).toBe("payload_too_large");
  });

  it("accepts a file exactly at the usable upload budget", () => {
    expect(validateDocumentFile({ type: "text/plain", size: MAX_UPLOAD_BYTES })).toBeNull();
  });
});

describe("uploadDocument / retryDocument network behaviour", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(responses: (() => Response)[]) {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    let index = 0;
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      const next = responses[index];
      index += 1;
      if (!next) throw new Error(`unexpected extra fetch call to ${String(url)}`);
      return next();
    });
    return calls;
  }

  const ok = (body: Record<string, unknown>) =>
    new Response(JSON.stringify(body), { status: 202, headers: { "Content-Type": "application/json" } });

  const unauthorized = () =>
    new Response(JSON.stringify({ error: { code: "unauthenticated", message: "expired" } }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });

  it("posts the file as multipart form data with a Bearer token, no manual Content-Type", async () => {
    const calls = stubFetch([() => ok({ id: "d1", title: "a.pdf", status: "pending", error: null })]);
    const file = new File(["hello"], "a.pdf", { type: "application/pdf" });

    const result = await uploadDocument(file, "", {
      accessToken: "tok",
      apiUrl: "http://api.test",
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://api.test/api/v1/documents");
    expect(calls[0].init?.body).toBeInstanceOf(FormData);
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok");
    // A manual Content-Type here would strip FormData's own boundary and
    // break the server's multipart parser.
    expect(headers["Content-Type"]).toBeUndefined();
    expect(result).toEqual({ id: "d1", title: "a.pdf", status: "PENDING", error: null });
  });

  it("normalises the REST response's lowercase status to the GraphQL enum casing", async () => {
    stubFetch([() => ok({ id: "d1", title: "a.pdf", status: "ready", error: null })]);
    const file = new File(["hello"], "a.pdf", { type: "application/pdf" });

    const result = await uploadDocument(file, "", { accessToken: "tok", apiUrl: "http://api.test" });

    expect(result.status).toBe("READY");
  });

  it("omits the title field when none is given", async () => {
    const calls = stubFetch([() => ok({ id: "d1", title: "a.pdf", status: "pending", error: null })]);
    const file = new File(["hello"], "a.pdf", { type: "application/pdf" });

    await uploadDocument(file, "   ", { accessToken: "tok", apiUrl: "http://api.test" });

    const form = calls[0].init?.body as FormData;
    expect(form.has("title")).toBe(false);
  });

  it("refreshes once and retries the upload after a 401", async () => {
    const calls = stubFetch([
      unauthorized,
      () => new Response(JSON.stringify({ access_token: "fresh", expires_in: 900 })),
      () => ok({ id: "d1", title: "a.pdf", status: "pending", error: null }),
    ]);
    const tokens: string[] = [];
    const file = new File(["hello"], "a.pdf", { type: "application/pdf" });

    await uploadDocument(file, "", {
      accessToken: "stale",
      apiUrl: "http://api.test",
      onAccessToken: (t) => tokens.push(t),
    });

    expect(calls.map((c) => c.url)).toEqual([
      "http://api.test/api/v1/documents",
      "/api/v1/auth/refresh",
      "http://api.test/api/v1/documents",
    ]);
    const retryHeaders = calls[2].init?.headers as Record<string, string>;
    expect(retryHeaders.Authorization).toBe("Bearer fresh");
    expect(tokens).toEqual(["fresh"]);
  });

  it("throws the server's error envelope on a non-2xx response instead of retrying forever", async () => {
    stubFetch([
      () =>
        new Response(JSON.stringify({ error: { code: "unsupported_document_type", message: "nope" } }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    ]);
    const file = new File(["hello"], "a.zip", { type: "application/zip" });

    await expect(
      uploadDocument(file, "", { accessToken: "tok", apiUrl: "http://api.test" }),
    ).rejects.toEqual({ code: "unsupported_document_type", message: "nope" });
  });

  it("posts a retry with no body to the retry endpoint", async () => {
    const calls = stubFetch([() => ok({ id: "d1", title: "a.pdf", status: "pending", error: null })]);

    await retryDocument("d1", { accessToken: "tok", apiUrl: "http://api.test" });

    expect(calls[0].url).toBe("http://api.test/api/v1/documents/d1/retry");
    expect(calls[0].init?.body).toBeUndefined();
  });

  it("surfaces a 409 from retrying a document that is not retryable", async () => {
    stubFetch([
      () =>
        new Response(JSON.stringify({ error: { code: "conflict", message: "cannot retry" } }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
    ]);

    await expect(
      retryDocument("d1", { accessToken: "tok", apiUrl: "http://api.test" }),
    ).rejects.toEqual({ code: "conflict", message: "cannot retry" });
  });
});
