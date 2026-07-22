import type { NextConfig } from "next";

const kioskApi = process.env.KIOSK_API_URL ?? "http://127.0.0.1:8080";

const nextConfig: NextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },
  experimental: {
    forceSwcTransforms: false,
  },
  compiler: undefined,
  async rewrites() {
    return [
      { source: "/api/map", destination: `${kioskApi}/api/map` },
      { source: "/api/upload-status", destination: `${kioskApi}/api/upload-status` },
      { source: "/api/image", destination: `${kioskApi}/api/image` },
      { source: "/api/upload-poster", destination: `${kioskApi}/api/upload-poster` },
      { source: "/api/facebook", destination: `${kioskApi}/api/facebook` },
      { source: "/api/network-ip", destination: `${kioskApi}/api/network-ip` },
      { source: "/api/weather", destination: `${kioskApi}/api/weather` },
      { source: "/api/eye-color", destination: `${kioskApi}/api/eye-color` },
      { source: "/api/voice-config", destination: `${kioskApi}/api/voice-config` },
    ];
  },
};

export default nextConfig;
