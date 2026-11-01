import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_API_BASE: "/api/v1", NEXT_PUBLIC_DEMO_MODE: "false" },
  ...(process.env.KONTUR_BUILD_CPUS === "1" ? { experimental: { cpus: 1, staticGenerationMaxConcurrency: 1 } } : {}),
  distDir: process.env.KONTUR_QA_DIST_DIR ?? ".next",
  poweredByHeader: false,
  devIndicators: false,
};

export default nextConfig;
