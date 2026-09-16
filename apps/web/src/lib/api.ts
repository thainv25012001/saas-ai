/**
 * Where the API actually lives. Used for the calls that authenticate with a
 * Bearer token and reach it directly: the GraphQL endpoint and the SSE chat
 * stream. The auth routes deliberately do not use it — see `apiFetch`.
 */
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ApiError = { code: string; message: string };

/**
 * The client for the auth routes, and only the auth routes.
 *
 * The path is relative on purpose, so the request goes to this app's own
 * origin and Next proxies it on to the API (`auth-proxy.ts` has the rules and
 * the reasoning). That is what keeps the refresh token a first-party cookie:
 * prefix this with `API_URL` again and, in any deployment where the two are on
 * different hosts, the cookie becomes unreadable to `middleware.ts` and
 * unsendable by the browser — a successful login that lands back on the login
 * form.
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error: ApiError = body?.error ?? {
      code: "network_error",
      message: "Something went wrong. Please try again.",
    };
    throw error;
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
