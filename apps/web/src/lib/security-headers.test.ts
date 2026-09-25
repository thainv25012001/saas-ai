import { describe, expect, it } from "vitest";
import { securityHeaderRules } from "./security-headers";

const APP_SOURCE = "/((?!embed/).*)";
const EMBED_SOURCE = "/embed/:path*";

/** One rule, by its `source`, flattened to a lookup. */
function headersFor(source: string) {
  const rules = securityHeaderRules();
  expect(rules).toHaveLength(2);
  const rule = rules.find((r) => r.source === source);
  if (!rule) throw new Error(`no rule for ${source}`);
  return Object.fromEntries(rule.headers.map(({ key, value }) => [key, value]));
}

/** The rule every non-embed route gets. */
function headers() {
  return headersFor(APP_SOURCE);
}

/** Mirrors how Next matches a rule's `source` for a path with no params. */
function matches(source: string, path: string): boolean {
  if (source === EMBED_SOURCE) return path.startsWith("/embed/") || path === "/embed";
  return new RegExp(`^${source}$`).test(path);
}

describe("securityHeaderRules", () => {
  it("covers every route except the embed page, not just the ones behind the middleware", () => {
    // middleware.ts is matched to /dashboard and /embed. Headers set there
    // would leave the marketing pages and the login form bare, which is
    // exactly where a clickjacking frame would be pointed.
    expect(securityHeaderRules()[0].source).toBe(APP_SOURCE);
    for (const path of ["/", "/login", "/dashboard", "/dashboard/agents/1", "/embedded"]) {
      expect(matches(APP_SOURCE, path)).toBe(true);
    }
    expect(matches(APP_SOURCE, "/embed/pk_abc")).toBe(false);
  });

  it("keeps /dashboard unframeable", () => {
    expect(matches(APP_SOURCE, "/dashboard")).toBe(true);
    expect(headers()["Content-Security-Policy"]).toBe("frame-ancestors 'none'");
  });

  it("leaves framing to the middleware on /embed, and nothing else", () => {
    // The embed page is framed by the business's own site; its
    // frame-ancestors is per agent and set in middleware.ts. A second,
    // static CSP here would be intersected with it and refuse every frame.
    const embed = headersFor(EMBED_SOURCE);
    expect(embed["Content-Security-Policy"]).toBeUndefined();
    expect(embed["X-Frame-Options"]).toBeUndefined();

    const rest = { ...headers() };
    delete rest["Content-Security-Policy"];
    delete rest["X-Frame-Options"];
    expect(embed).toEqual(rest);
  });

  it("refuses to be framed", () => {
    expect(headers()["Content-Security-Policy"]).toBe("frame-ancestors 'none'");
    expect(headers()["X-Frame-Options"]).toBe("DENY");
  });

  it("forbids MIME sniffing", () => {
    expect(headers()["X-Content-Type-Options"]).toBe("nosniff");
  });

  it("states the referrer policy instead of relying on the browser default", () => {
    // The value matches what browsers already default to. Sending it makes
    // the behaviour a decision this repo owns rather than one a future
    // browser release could change underneath it.
    expect(headers()["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
  });

  it("sends HSTS for two years across subdomains", () => {
    expect(headers()["Strict-Transport-Security"]).toBe("max-age=63072000; includeSubDomains");
  });

  it("does not submit the host to the HSTS preload list", () => {
    // preload is a commitment baked into browser binaries and slow to undo.
    // Adding it has to be a deliberate, separate decision.
    expect(headers()["Strict-Transport-Security"]).not.toContain("preload");
  });

  it("denies the browser features this app never uses", () => {
    expect(headers()["Permissions-Policy"]).toBe("camera=(), microphone=(), geolocation=()");
  });

  it("names no header twice", () => {
    for (const rule of securityHeaderRules()) {
      const keys = rule.headers.map(({ key }) => key);
      expect(keys).toHaveLength(new Set(keys).size);
    }
  });
});
