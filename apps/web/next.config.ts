import type { NextConfig } from "next";
import { securityHeaderRules } from "./src/lib/security-headers";

const nextConfig: NextConfig = {
  headers: async () => securityHeaderRules(),
};

export default nextConfig;
