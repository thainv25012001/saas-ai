import { describe, expect, it } from "vitest";
import {
  AUTH_PATH_PREFIX,
  authProxyRewrites,
  proxyTargetUrl,
  widgetConfigRewrites,
} from "./auth-proxy";

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

describe("widgetConfigRewrites", () => {
  it("serves the loader's config check from this app's origin", () => {
    // widget.js only knows the origin it was loaded from, so the launcher's
    // pre-draw config request has to be answerable there.
    expect(widgetConfigRewrites(`${API}/`)).toEqual([
      {
        source: "/api/v1/widget/:key/config",
        destination: `${API}/api/v1/widget/:key/config`,
      },
    ]);
  });

  it("proxies the config route only, not the rest of the widget API", () => {
    const [rule] = widgetConfigRewrites(API);
    expect(rule.source.endsWith("/config")).toBe(true);
    expect(rule.source).not.toContain(":path*");
  });
});

describe("proxyTargetUrl", () => {
  it("dials the internal URL when one is set", () => {
    // Under Compose the browser reaches the API on the published host port
    // while the Next server, which is what actually performs the rewrite, has
    // to use the service name. Feed it the browser's URL and every login is a
    // 500: `localhost:8000` inside the web container is the web container.
    expect(proxyTargetUrl("http://api:8000", "http://localhost:8000")).toBe(
      "http://api:8000",
    );
  });

  it("falls back to the browser-facing URL when there is no internal one", () => {
    // Vercel + Render: one public URL serves both vantage points, so
    // API_INTERNAL_URL stays unset there and nothing has to be configured.
    expect(proxyTargetUrl(undefined, API)).toBe(API);
  });

  it("ignores an internal URL that is set but blank", () => {
    // Compose writes `API_INTERNAL_URL: ${API_INTERNAL_URL:-}` style empty
    // strings, and an empty destination would rewrite every auth route to a
    // relative path pointing back at this app — an infinite proxy loop.
    expect(proxyTargetUrl("   ", API)).toBe(API);
  });
});
