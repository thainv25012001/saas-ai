import type { NextConfig } from "next";
import { API_URL } from "./src/lib/api";
import {
  authProxyRewrites,
  proxyTargetUrl,
  widgetConfigRewrites,
} from "./src/lib/auth-proxy";
import { securityHeaderRules } from "./src/lib/security-headers";

const nextConfig: NextConfig = {
  headers: async () => securityHeaderRules(),
  rewrites: async () => {
    const apiUrl = proxyTargetUrl(process.env.API_INTERNAL_URL, API_URL);
    return [...authProxyRewrites(apiUrl), ...widgetConfigRewrites(apiUrl)];
  },
};

export default nextConfig;
