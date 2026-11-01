import type { NextConfig } from "next";

const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  poweredByHeader: false,
  reactStrictMode: true,
  devIndicators: false,
  images: { unoptimized: true },
  // Always define the build-time constant so production dead-code removes QA IPC.
  env: { NEXT_PUBLIC_KONTUR_TEST_ONLY: process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY === "1" ? "1" : "0" },
  experimental: { cpus: 1 },
};

export default config;
