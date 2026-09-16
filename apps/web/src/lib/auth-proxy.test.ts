import { describe, expect, it } from "vitest";
import { AUTH_PATH_PREFIX, authProxyRewrites } from "./auth-proxy";

const API = "https://saas-ai-api.onrender.com";

describe("authProxyRewrites", () => {
  it("serves every auth route from this app's own origin", () => {
    // The whole point: a relative source means the browser sends the request
    // to the web origin, so the Set-Cookie that comes back belongs to the web
    // origin too. A destination-only rule would change nothing.
    const [rule] = authProxyRewrites(API);
    expect(rule.source).toBe(`${AUTH_PATH_PREFIX}/:path*`);
    expect(rule.destination).toBe(`${API}${AUTH_PATH_PREFIX}/:path*`);
  });

  it("proxies the auth routes and nothing else", () => {
    // GraphQL and /api/v1/chat/stream carry a Bearer token and no cookie, so
    // they have nothing to gain from the hop - and routing the stream through
    // the host's proxy layer would saddle it with that layer's timeout.
    const sources = authProxyRewrites(API).map((rule) => rule.source);
    expect(sources).toEqual([`${AUTH_PATH_PREFIX}/:path*`]);
    expect(sources.some((source) => source.startsWith("/graphql"))).toBe(false);
  });

  it("does not double the slash when the API URL has a trailing one", () => {
    // NEXT_PUBLIC_API_URL is typed into a dashboard by hand; a trailing slash
    // there would otherwise produce //api/v1/auth and a 404 on every login.
    expect(authProxyRewrites(`${API}/`)[0].destination).toBe(
      `${API}${AUTH_PATH_PREFIX}/:path*`,
    );
  });
});
