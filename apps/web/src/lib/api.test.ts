import { afterEach, describe, expect, it, vi } from "vitest";
import { API_URL, apiFetch } from "./api";

function stubFetch(response: Partial<Response> = {}) {
  // Typed as `fetch` itself so `mock.calls` carries fetch's own argument
  // types - which is what lets the assertions below read [url, init].
  const fetchMock = vi.fn<typeof fetch>(async () => ({
    ok: true,
    status: 200,
    json: async () => ({}),
    ...response,
  }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetch", () => {
  it("keeps auth calls on this origin so the cookie comes back first-party", async () => {
    // A relative URL is the entire fix for the production login loop: the
    // request goes to the web origin, Next proxies it (see auth-proxy.ts),
    // and the Set-Cookie lands on the host that middleware.ts reads. Prefix
    // this with API_URL again and the cookie goes back to being unreadable.
    const fetchMock = stubFetch();
    await apiFetch("/api/v1/auth/refresh", { method: "POST" });

    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/auth/refresh");
    expect(url).not.toContain(API_URL);
  });

  it("sends the cookie", async () => {
    const fetchMock = stubFetch();
    await apiFetch("/api/v1/auth/me");

    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
  });

  it("surfaces the API's error envelope", async () => {
    stubFetch({
      ok: false,
      status: 401,
      json: async () => ({ error: { code: "invalid_credentials", message: "Nope" } }),
    });

    await expect(apiFetch("/api/v1/auth/login", { method: "POST" })).rejects.toEqual({
      code: "invalid_credentials",
      message: "Nope",
    });
  });
});
