/**
 * The security headers every response from the web app carries.
 *
 * Lives here rather than inline in `next.config.ts` for one practical reason:
 * vitest only collects `src/**`, so a list written in the config file could
 * not be tested at all, and a header quietly dropped in a future edit would
 * reach production unnoticed.
 *
 * Applied through `headers()` in the config rather than through
 * `middleware.ts`. That middleware is matched to `/dashboard` and `/embed`
 * only, and widening it to every route would put a function invocation in
 * front of all static traffic purely to set six constants.
 *
 * The one exception is framing on `/embed/*`, the chat widget's page, which a
 * business's own site frames: its `frame-ancestors` is per agent, so
 * `middleware.ts` sets it from `lib/frame-policy.ts`. A static CSP here as
 * well would be enforced alongside it (browsers apply every policy they
 * receive), refusing every frame, so the embed rule carries everything else
 * and nothing about framing.
 */

type HeaderRule = {
  source: string;
  headers: { key: string; value: string }[];
};

/** Every header except the two about framing, which differ by route. */
const COMMON_HEADERS: HeaderRule["headers"] = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  // The same value browsers already apply when a server says nothing.
  // Stating it makes the behaviour something this repo decides rather
  // than something a future browser release decides for it.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Two years, the HSTS preload floor. No `preload` token: that submits
  // the hostname to a list compiled into browser binaries, which takes
  // months to reverse and should be its own decision.
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  // Nothing in this app asks for a camera, a microphone or a location.
  // Saying so stops an injected script or embed from asking either.
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

export function securityHeaderRules(): HeaderRule[] {
  return [
    {
      // Every path except `/embed/...` (a negative lookahead: Next matches
      // `source` as a path-to-regexp pattern, which accepts a regex group).
      source: "/((?!embed/).*)",
      headers: [
        // Only `frame-ancestors` for now. A `script-src` policy is what would
        // actually blunt XSS, but Next's hydration relies on inline scripts,
        // so it needs either a per-request nonce (which costs static
        // rendering on every route) or `unsafe-inline` (which gives back most
        // of the protection). That is a deliberate, separate piece of work.
        { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        // Redundant with frame-ancestors on any browser still getting
        // updates; kept for the ones that are not, at a cost of one line.
        { key: "X-Frame-Options", value: "DENY" },
        ...COMMON_HEADERS,
      ],
    },
    {
      source: "/embed/:path*",
      headers: [...COMMON_HEADERS],
    },
  ];
}
