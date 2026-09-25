/**
 * Who may frame `/embed/<publicKey>` -- the per-agent `frame-ancestors`
 * policy `middleware.ts` sets (spec
 * docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §6).
 *
 * The API already normalises `allowed_origins` before storing them (Review
 * Focus 4), but this header is where a bad value would actually do harm, so
 * the builder refuses anything that is not a bare `scheme://host[:port]`
 * even if the API returned it. A `*`, a path or a stray `;` never reaches
 * the browser.
 *
 * Middleware runs on the edge runtime: `fetch` and a module-level `Map` only,
 * no Node APIs, and no Next fetch cache (it does not apply there), hence the
 * small cache below.
 */

const BARE_ORIGIN = /^https?:\/\/[a-z0-9.-]+(:\d{1,5})?$/;

export function isBareOrigin(value: string): boolean {
  return BARE_ORIGIN.test(value.toLowerCase());
}

export function frameAncestorsHeader(origins: string[]): string {
  const allowed = origins.filter(isBareOrigin).map((origin) => origin.toLowerCase());
  return ["frame-ancestors", "'self'", ...allowed].join(" ");
}

const CACHE_TTL_MS = 30_000;
const CACHE_MAX_ENTRIES = 500;
/** A slow API must not hold the embed page hostage; a timeout is handled
 * like any other failure (the last known answer, else `'self'` only). */
const FETCH_TIMEOUT_MS = 3_000;

/** Proves to the API that this request is our own middleware, which skips
 * the route's per-key limit (a public key's budget is otherwise anyone's to
 * spend). Server-only: never a `NEXT_PUBLIC_` variable. */
export const FRAME_SECRET_HEADER = "X-Widget-Frame-Secret";

function frameSecretHeaders(): Record<string, string> {
  const secret = process.env.WIDGET_FRAME_POLICY_SECRET;
  return secret?.trim() ? { [FRAME_SECRET_HEADER]: secret } : {};
}

type CacheEntry = { origins: string[]; storedAt: number };

const cache = new Map<string, CacheEntry>();

function remember(publicKey: string, origins: string[], now: number): void {
  // Re-inserting moves the key to the end, so insertion order stays oldest
  // first and eviction below drops the least recently stored answer.
  cache.delete(publicKey);
  while (cache.size >= CACHE_MAX_ENTRIES) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
  cache.set(publicKey, { origins, storedAt: now });
}

/**
 * The agent's allowed origins, from `GET /api/v1/widget/{key}/frame-policy`.
 *
 * A fresh answer (under 30 s old) is served from the cache. On any failure
 * -- network, timeout, a non-200 including 429, a malformed body -- this
 * serves the key's last successful answer however old it is: this server is
 * the API's only caller for this route, so a blip or a rate limit must not
 * unframe a widget that is live on a customer's site. Only a key that has
 * never been answered successfully falls back to `[]` (framed by this app
 * only). A failure is never stored, so the next request asks again.
 */
export async function fetchFrameOrigins(
  apiBase: string,
  publicKey: string,
  now: () => number = Date.now,
): Promise<string[]> {
  const hit = cache.get(publicKey);
  if (hit && now() - hit.storedAt < CACHE_TTL_MS) return hit.origins;
  const fallback = hit?.origins ?? [];

  try {
    const url = `${apiBase.replace(/\/+$/, "")}/api/v1/widget/${encodeURIComponent(publicKey)}/frame-policy`;
    const response = await fetch(url, {
      cache: "no-store",
      headers: frameSecretHeaders(),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!response.ok) return fallback;
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null) return fallback;
    const raw = (body as { allowed_origins?: unknown }).allowed_origins;
    if (!Array.isArray(raw)) return fallback;
    const origins = raw.filter((value): value is string => typeof value === "string");
    remember(publicKey, origins, now());
    return origins;
  } catch {
    return fallback;
  }
}
