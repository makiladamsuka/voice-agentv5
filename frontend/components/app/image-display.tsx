"use client";

import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/utils";
import { useEffect, useState, Suspense } from "react";
import dynamic from "next/dynamic";
import LoadingOverlay from "@/components/ui/LoadingOverlay";

// Lazy load the 3D navigation map (heavy Three.js dependency)
const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

const MotionOverlay = motion.create("div");

interface ImageData {
  type: string;
  category: string;
  url: string;
  caption: string;
}

interface NavigationData {
  destination: string;
  floor: string;
  path: number[][];
  path_ids?: string[];
  nodes: any[];
  buildings: any;
}

/**
 * NLU kiosk: image and navigation overlays are driven by NLU WebSocket
 * action payloads dispatched directly from kiosk-view.tsx. This component
 * is a no-op stub retained for API compatibility.
 */
export function ImageDisplay({
  ignoreNavigation = false,
}: {
  ignoreNavigation?: boolean;
}) {
  // In NLU mode all overlays are managed inside kiosk-view.tsx via useNluAdapter.
  return null;
}
