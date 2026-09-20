/**
 * Where the API actually lives. Used for the calls that authenticate with a
 * Bearer token and reach it directly: the GraphQL endpoint and the SSE chat
 * stream. The auth routes deliberately do not use it — see `apiFetch`.
 */
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ApiError = { code: string; message: string };

const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

/**
 * The API's `{error: {code, message}}` envelope, parsed once.
 *
 * `fallbackCode` is the caller's, because what a malformed or non-JSON error
 * body *means* depends on who asked: for `apiFetch`'s same-origin auth routes
 * it is most likely the proxy or the network, while a direct call to the API
 * that returns an unparseable error has reached the server and failed there.
 * Everything else about the shape is identical, so it lives here rather than
 * being re-derived per module -- the two copies this replaced had already
 * drifted to different fallback codes for no reasoned difference.
 */
export async function parseErrorEnvelope(
  response: Response,
  fallbackCode: string,
): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const error =
    body && typeof body === "object" && "error" in body
      ? (body as { error?: unknown }).error
      : null;
  if (error && typeof error === "object" && "code" in error && "message" in error) {
    const { code, message } = error as { code: unknown; message: unknown };
    return {
      code: typeof code === "string" ? code : fallbackCode,
      message: typeof message === "string" ? message : GENERIC_ERROR_MESSAGE,
    };
  }
  return { code: fallbackCode, message: GENERIC_ERROR_MESSAGE };
}

/**
 * One request with a Bearer token, refreshed and retried exactly once on a
 * 401.
 *
 * GraphQL recovers from an expired access token silently through urql's
 * `authExchange`; the calls that bypass urql -- the SSE chat stream and the
 * multipart document uploads -- would otherwise be the only actions in the
 * dashboard that fail in a tab left open past the token's lifetime.
 *
 * ONE retry, never a loop: retrying a refresh that keeps coming back 401 is
 * how you build one. If the refresh itself fails there is no session left to
 * salvage, so the original 401 is returned untouched, which is what moves the
 * user back to /login.
 */
export async function fetchWithRefresh(
  send: (token: string) => Promise<Response>,
  accessToken: string,
  onAccessToken?: (token: string) => void,
): Promise<Response> {
  const response = await send(accessToken);
  if (response.status !== 401) return response;

  let refreshed: string | null = null;
  try {
    const tokens = await apiFetch<{ access_token: string }>("/api/v1/auth/refresh", {
      method: "POST",
    });
    refreshed = tokens.access_token;
  } catch {
    refreshed = null;
  }
  if (refreshed === null) return response;

  onAccessToken?.(refreshed);
  return send(refreshed);
}

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
    throw await parseErrorEnvelope(response, "network_error");
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
