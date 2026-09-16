import type { NextConfig } from "next";
import { API_URL } from "./src/lib/api";
import { authProxyRewrites } from "./src/lib/auth-proxy";
import { securityHeaderRules } from "./src/lib/security-headers";

const nextConfig: NextConfig = {
  headers: async () => securityHeaderRules(),
  rewrites: async () => authProxyRewrites(API_URL),
};

export default nextConfig;
