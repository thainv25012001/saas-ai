export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ApiError = { code: string; message: string };

/** Every call sends credentials: the refresh token is an httpOnly cookie. */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
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
