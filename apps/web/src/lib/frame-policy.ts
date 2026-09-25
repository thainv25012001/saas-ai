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
/** A slow API must not hold the embed page hostage; on timeout the page is
 * framed for `'self'` only, like any other failure. */
const FETCH_TIMEOUT_MS = 3_000;

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
 * Any failure answers `[]` -- framed by this app only -- and is not cached,
 * so a blip does not lock a customer's site out for the whole TTL.
 */
export async function fetchFrameOrigins(
  apiBase: string,
  publicKey: string,
  now: () => number = Date.now,
): Promise<string[]> {
  const hit = cache.get(publicKey);
  if (hit && now() - hit.storedAt < CACHE_TTL_MS) return hit.origins;

  try {
    const url = `${apiBase.replace(/\/+$/, "")}/api/v1/widget/${encodeURIComponent(publicKey)}/frame-policy`;
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!response.ok) return [];
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null) return [];
    const raw = (body as { allowed_origins?: unknown }).allowed_origins;
    if (!Array.isArray(raw)) return [];
    const origins = raw.filter((value): value is string => typeof value === "string");
    remember(publicKey, origins, now());
    return origins;
  } catch {
    return [];
  }
}
