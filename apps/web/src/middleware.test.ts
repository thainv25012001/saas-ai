import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { middleware } from "./middleware";

afterEach(() => {
  vi.unstubAllGlobals();
});

function request(path: string, cookie?: string): NextRequest {
  return new NextRequest(`http://localhost:3000${path}`, {
    headers: cookie ? { cookie } : {},
  });
}

describe("middleware", () => {
  it("sends a /dashboard visitor with no session to /login", async () => {
    const response = await middleware(request("/dashboard/agents"));
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("http://localhost:3000/login");
  });

  it("lets a /dashboard visitor with a session through", async () => {
    const response = await middleware(request("/dashboard", "refresh_token=abc"));
    expect(response.headers.get("location")).toBeNull();
  });

  it("frames /embed/<key> for that agent's origins, with no session and no redirect", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ allowed_origins: ["https://shop.example", "*"] }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await middleware(request("/embed/pk_mw_one"));

    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("Content-Security-Policy")).toBe(
      "frame-ancestors 'self' https://shop.example",
    );
    expect(response.headers.get("X-Frame-Options")).toBeNull();
    expect(String(fetchMock.mock.calls[0][0])).toMatch(
      /\/api\/v1\/widget\/pk_mw_one\/frame-policy$/,
    );
  });

  it("frames /embed for 'self' only when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));

    const response = await middleware(request("/embed/pk_mw_down"));

    expect(response.headers.get("Content-Security-Policy")).toBe("frame-ancestors 'self'");
  });

  it("does not ask the API about a key that cannot be one", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await middleware(request("/embed/..%2Fadmin"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(response.headers.get("Content-Security-Policy")).toBe("frame-ancestors 'self'");
  });
});
