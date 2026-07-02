"use client";

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";

// Organic blob path generation using sine wave distortion
function generateBlobPath(
  cx: number,
  cy: number,
  r: number,
  points: number,
  seed: number
): string {
  const step = (Math.PI * 2) / points;
  const pts: [number, number][] = [];

  for (let i = 0; i < points; i++) {
    const angle = step * i + seed;
    const noise =
      0.75 +
      0.25 * Math.sin(seed * 3.1 + i * 1.7) +
      0.15 * Math.cos(seed * 2.3 + i * 0.9) +
      0.1 * Math.sin(seed * 5.7 + i * 2.1);
    const rad = r * noise;
    pts.push([cx + rad * Math.cos(angle), cy + rad * Math.sin(angle)]);
  }

  // Build smooth closed cubic bezier
  let d = `M ${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
  for (let i = 0; i < pts.length; i++) {
    const curr = pts[i];
    const next = pts[(i + 1) % pts.length];
    const cpX = (curr[0] + next[0]) / 2;
    const cpY = (curr[1] + next[1]) / 2;
    d += ` Q ${curr[0].toFixed(2)} ${curr[1].toFixed(2)}, ${cpX.toFixed(2)} ${cpY.toFixed(2)}`;
  }
  d += " Z";
  return d;
}

interface MicBlobOverlayProps {
  /** Whether the overlay is visible */
  visible: boolean;
  /** Called once connection is ready and user can start chatting */
  onReady?: () => void;
  /** Whether the agent has connected (triggers snap-to-chat) */
  isConnected: boolean;
}

export function MicBlobOverlay({
  visible,
  isConnected,
}: MicBlobOverlayProps) {
  const [seed, setSeed] = useState(0);
  const [blobPath, setBlobPath] = useState("");
  const [innerPath, setInnerPath] = useState("");
  const [outerPath, setOuterPath] = useState("");
  const animRef = useRef<number | null>(null);
  const startTime = useRef(Date.now());

  // Animate the blob on every frame
  useEffect(() => {
    if (!visible) return;
    startTime.current = Date.now();

    const animate = () => {
      const t = (Date.now() - startTime.current) / 1000;
      const s = t * 0.55; // main seed
      setBlobPath(generateBlobPath(200, 200, 140, 12, s));
      setInnerPath(generateBlobPath(200, 200, 100, 10, s * 1.3 + 1.1));
      setOuterPath(generateBlobPath(200, 200, 175, 8, s * 0.7 - 0.5));
      setSeed(t);
      animRef.current = requestAnimationFrame(animate);
    };

    animRef.current = requestAnimationFrame(animate);
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [visible]);

  // Hue rotation follows time for the iridescent colour shift
  const hue1 = (seed * 40) % 360;
  const hue2 = (seed * 40 + 120) % 360;
  const hue3 = (seed * 40 + 240) % 360;

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key="blob-overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0, scale: 1.08 }}
          transition={{ duration: 0.35, ease: "easeInOut" }}
          className="fixed inset-0 z-[200] flex flex-col items-center justify-center"
          style={{ background: "var(--background)" }}
        >
          {/* Ambient background glow */}
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              background: `radial-gradient(ellipse 70% 60% at 50% 50%,
                hsl(${hue1}, 90%, 60%, 0.18),
                hsl(${hue2}, 90%, 55%, 0.10),
                transparent 70%)`,
              transition: "background 0.2s",
            }}
          />

          {/* SVG blob stack */}
          <div className="relative flex items-center justify-center select-none">
            {/* SVG filter defs */}
            <svg width="0" height="0" className="absolute">
              <defs>
                <filter id="gooey" x="-50%" y="-50%" width="200%" height="200%">
                  <feGaussianBlur in="SourceGraphic" stdDeviation="8" result="blur" />
                  <feColorMatrix
                    in="blur"
                    mode="matrix"
                    values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 22 -9"
                    result="goo"
                  />
                </filter>
                <filter id="soft-blur">
                  <feGaussianBlur in="SourceGraphic" stdDeviation="3" />
                </filter>
              </defs>
            </svg>

            {/* Main animated blob SVG */}
            <svg
              viewBox="0 0 400 400"
              width={340}
              height={340}
              className="relative z-10"
              style={{ filter: "url(#gooey)" }}
            >
              {/* Outer ghost layer */}
              <path
                d={outerPath}
                fill={`hsl(${hue3}, 85%, 65%)`}
                opacity={0.25}
              />
              {/* Main body */}
              <path
                d={blobPath}
                fill={`hsl(${hue1}, 90%, 58%)`}
                opacity={0.85}
              />
              {/* Inner highlight */}
              <path
                d={innerPath}
                fill={`hsl(${hue2}, 95%, 72%)`}
                opacity={0.6}
              />
              {/* Specular highlight */}
              <ellipse
                cx={175}
                cy={155}
                rx={38}
                ry={28}
                fill="white"
                opacity={0.35}
                style={{
                  transform: `rotate(${seed * 18}deg)`,
                  transformOrigin: "200px 200px",
                }}
              />
            </svg>

            {/* Mic icon centred on blob */}
            <div className="absolute inset-0 flex items-center justify-center z-20">
              <div className="flex flex-col items-center gap-3">
                <span
                  className="material-symbols-outlined text-white drop-shadow-lg"
                  style={{ fontSize: 52, filter: "drop-shadow(0 2px 8px rgba(0,0,0,0.35))" }}
                >
                  mic
                </span>
              </div>
            </div>
          </div>

          {/* Status label */}
          <motion.div
            key={isConnected ? "connected" : "connecting"}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-6 text-on-surface/70 text-[15px] font-semibold tracking-wide"
          >
            {isConnected ? "Connected — starting…" : "Connecting…"}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
