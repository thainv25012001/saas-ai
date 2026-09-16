import type { NextConfig } from "next";
import { API_URL } from "./src/lib/api";
import { authProxyRewrites, proxyTargetUrl } from "./src/lib/auth-proxy";
import { securityHeaderRules } from "./src/lib/security-headers";

const nextConfig: NextConfig = {
  headers: async () => securityHeaderRules(),
  rewrites: async () =>
    authProxyRewrites(proxyTargetUrl(process.env.API_INTERNAL_URL, API_URL)),
};

export default nextConfig;
