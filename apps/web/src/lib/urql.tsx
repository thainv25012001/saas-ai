"use client";

import { useMemo } from "react";
import { Client, Provider, cacheExchange, fetchExchange } from "urql";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function UrqlProvider({ children }: { children: React.ReactNode }) {
  const { accessToken } = useAuth();

  const client = useMemo(
    () =>
      new Client({
        url: `${API_URL}/graphql`,
        exchanges: [cacheExchange, fetchExchange],
        fetchOptions: () => ({
          credentials: "include",
          headers: (accessToken
            ? { Authorization: `Bearer ${accessToken}` }
            : {}) as HeadersInit,
        }),
      }),
    // A new client per token keeps the cache from serving one tenant's data
    // to the next session after a re-login.
    [accessToken],
  );

  return <Provider value={client}>{children}</Provider>;
}
