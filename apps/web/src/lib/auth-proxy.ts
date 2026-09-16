/**
 * The auth routes are served from this app's own origin and proxied to the
 * API from there, rather than being called across origins from the browser.
 *
 * The refresh token is an httpOnly cookie, and a cookie belongs to exactly one
 * host. In production the web app is on Vercel and the API is on Render — two
 * different registrable domains, both on the Public Suffix List, so they
 * cannot be made to share a cookie even with a `Domain` attribute. A cookie
 * the API set was therefore invisible here twice over: `middleware.ts` could
 * not read it to admit anyone to /dashboard, and the browser would not attach
 * it to a cross-site POST /api/v1/auth/refresh. Locally the two share the host
 * `localhost` (cookies ignore ports), which is why it only broke once
 * deployed — a successful login landed straight back on the login form.
 *
 * Routing those calls through this origin makes the cookie first-party again:
 * the Set-Cookie comes back on the web host, SameSite=Lax is satisfied, and
 * the middleware guard sees the cookie it was written to look for.
 *
 * The auth routes and nothing else. They are the only ones that read the
 * cookie (`app/api/auth.py` is its only reader); GraphQL and the SSE chat
 * stream authenticate with a Bearer token, so they keep going straight to the
 * API rather than paying a proxy hop — which for a long-lived stream would
 * also mean inheriting the host's function timeout.
 */
export const AUTH_PATH_PREFIX = "/api/v1/auth";

export type RewriteRule = { source: string; destination: string };

export function authProxyRewrites(apiUrl: string): RewriteRule[] {
  return [
    {
      source: `${AUTH_PATH_PREFIX}/:path*`,
      destination: `${apiUrl.replace(/\/+$/, "")}${AUTH_PATH_PREFIX}/:path*`,
    },
  ];
}
