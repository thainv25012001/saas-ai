"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type TokenResponse = { access_token: string; expires_in: number };
type Me = {
  user_id: string;
  email: string;
  full_name: string;
  organization_id: string;
  organization_name: string;
  role: string;
};

type AuthValue = {
  accessToken: string | null;
  user: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  logout: () => Promise<void>;
};

type RegisterInput = {
  email: string;
  password: string;
  full_name: string;
  organization_name: string;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Held in memory only. localStorage is readable by any injected script.
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async (token: string) => {
    const me = await apiFetch<Me>("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    setUser(me);
  }, []);

  // On mount, trade the refresh cookie for an access token so a page reload
  // does not log the user out.
  useEffect(() => {
    (async () => {
      try {
        const tokens = await apiFetch<TokenResponse>("/api/v1/auth/refresh", {
          method: "POST",
        });
        setAccessToken(tokens.access_token);
        await loadUser(tokens.access_token);
      } catch {
        setAccessToken(null);
        setUser(null);
      } finally {
        setLoading(false);
      }
    })();
  }, [loadUser]);

  const authenticate = useCallback(
    async (path: string, body: unknown) => {
      const tokens = await apiFetch<TokenResponse>(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setAccessToken(tokens.access_token);
      await loadUser(tokens.access_token);
    },
    [loadUser],
  );

  const value: AuthValue = {
    accessToken,
    user,
    loading,
    login: (email, password) =>
      authenticate("/api/v1/auth/login", { email, password }),
    register: (input) => authenticate("/api/v1/auth/register", input),
    logout: async () => {
      await apiFetch<void>("/api/v1/auth/logout", { method: "POST" });
      setAccessToken(null);
      setUser(null);
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
