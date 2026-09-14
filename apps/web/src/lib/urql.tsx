"use client";

import { useMemo } from "react";
import { Client, Provider, cacheExchange, fetchExchange } from "urql";
import { authExchange } from "@urql/exchange-auth";
import { apiFetch, API_URL } from "@/lib/api";
import { useAuth, type TokenResponse } from "@/lib/auth";

export function UrqlProvider({ children }: { children: React.ReactNode }) {
  const { accessToken, setAccessToken, logout } = useAuth();

  const client = useMemo(
    () =>
      new Client({
        url: `${API_URL}/graphql`,
        exchanges: [
          cacheExchange,
          authExchange(async (utils) => {
            // The token lives in this closure, not in the `accessToken`
            // React value captured at client-creation time, so a refresh
            // that happens mid-session (not just on remount) is seen by the
            // very next retried operation instead of a stale render's copy.
            let token = accessToken;

            return {
              addAuthToOperation(operation) {
                if (!token) return operation;
                return utils.appendHeaders(operation, {
                  Authorization: `Bearer ${token}`,
                });
              },
              // Match on the GraphQL error code, not message text, so this
              // doesn't misfire on an unrelated error that merely mentions
              // "unauthenticated" in a message.
              didAuthError(error) {
                return error.graphQLErrors.some(
                  (e) => e.extensions?.code === "unauthenticated",
                );
              },
              async refreshAuth() {
                try {
                  const tokens = await apiFetch<TokenResponse>(
                    "/api/v1/auth/refresh",
                    { method: "POST" },
                  );
                  token = tokens.access_token;
                  // Push the rotated token into React state too: the next
                  // render rebuilds this Client (see the [accessToken]
                  // dependency below) with the fresh token as its starting
                  // point, and every other consumer of useAuth() sees it.
                  setAccessToken(tokens.access_token);
                } catch {
                  // The refresh token itself is gone or invalid - there is
                  // no session left to salvage. logout() clears React state
                  // in a `finally`, so it always drops the local session
                  // even if its own request to the API fails, and best-effort
                  // asks the API to drop the cookie too - either way, the
                  // next navigation lands on /login instead of retrying
                  // forever.
                  token = null;
                  await logout().catch(() => {});
                }
              },
            };
          }),
          fetchExchange,
        ],
        fetchOptions: () => ({
          credentials: "include",
        }),
      }),
    // A new client per token keeps the cache from serving one tenant's data
    // to the next session after a re-login, and re-seeds the auth exchange's
    // closure with whatever token is current whenever this Provider
    // re-renders with a different one (initial load, login, or a refresh
    // triggered from inside the exchange itself).
    [accessToken, setAccessToken, logout],
  );

  return <Provider value={client}>{children}</Provider>;
}
