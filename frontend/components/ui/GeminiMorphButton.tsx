"use client";

import { useEffect, useRef, useState } from "react";

// ─── Shape generation (M3 Polar Math) ──────────────────────────────────────────
const POINTS = 120; // 120 points is more than enough for perfectly smooth anti-aliased SVGs

function getPolarShape(numPetals: number, depth: number, phase: number = 0): number[] {
  const pts = [];
  for (let i = 0; i < POINTS; i++) {
    const theta = (i / POINTS) * Math.PI * 2;
    // Math for M3 expressive shapes (superellipses / polar roses)
    const r = 1 - depth + depth * Math.cos(numPetals * theta + phase);
    pts.push(r * Math.cos(theta), r * Math.sin(theta));
  }
  return pts;
}

const SHAPES: Record<string, number[]> = {
  // 8-petal soft scallop
  scallop8: getPolarShape(8, 0.08),
  
  // 4-petal smooth cushion (peaks on diagonals)
  cushion4: getPolarShape(4, 0.12, Math.PI),
  
  // 6-petal soft scallop
  scallop6: getPolarShape(6, 0.12),
  
  // 4-petal deep clover (peaks on axes)
  clover4: getPolarShape(4, 0.22),
  
  // 8-petal deep scallop
  scallop8_deep: getPolarShape(8, 0.14),
  
  // 5-petal pentagon
  pentagon: getPolarShape(5, 0.12, 0),
  
  // 6-petal hexagon
  hexagon: getPolarShape(6, 0.12, 0),
  
  // Ultra-smooth, low-depth blobs for minimal visual "noise"
  smooth4: getPolarShape(4, 0.05, Math.PI),
  smooth6: getPolarShape(6, 0.04, 0),
  smooth8: getPolarShape(8, 0.03, 0),
};

// Sequence using only smooth, low-depth shapes to reduce "visual noise"
const SEQUENCE = [
  "smooth4",
  "smooth6",
  "smooth8",
  "smooth6"
];

// ─── Math helpers ─────────────────────────────────────────────────────────────
function easeInOut(t: number) {
  // Cubic ease-in-out for much smoother transitions
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}
function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}
function lerpShapes(from: number[], to: number[], t: number) {
  return from.map((v, i) => lerp(v, to[i], t));
}
function buildPath(pts: number[], r: number, cx: number, cy: number): string {
  let d = "";
  for (let i = 0; i < pts.length / 2; i++) {
    const x = (pts[i * 2] * r + cx).toFixed(2);
    const y = (pts[i * 2 + 1] * r + cy).toFixed(2);
    d += (i === 0 ? "M " : "L ") + `${x},${y} `;
  }
  return d + "Z";
}

// ─── Component ────────────────────────────────────────────────────────────────
interface GeminiMorphButtonProps {
  /** Show morphing animation instead of mic icon */
  isAnimating: boolean;
  /** Whether LiveKit agent is already connected */
  isConnected: boolean;
  onClick: () => void;
}

export function GeminiMorphButton({
  isAnimating,
  isConnected,
  onClick,
}: GeminiMorphButtonProps) {
  const mainRef = useRef<SVGPathElement | null>(null);
  const animRef = useRef<number | null>(null);
  const segIdx = useRef(0);
  const segStart = useRef(0);
  const SEGMENT_MS = 4000; // Increased to 4000ms for ultra-smooth, calm morphing

  // ── Shape morph loop ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!isAnimating) {
      if (animRef.current) cancelAnimationFrame(animRef.current);
      return;
    }

    segIdx.current = 0;
    segStart.current = 0;

    const tick = (now: number) => {
      if (segStart.current === 0) segStart.current = now;

      const elapsed = now - segStart.current;
      const t = easeInOut(Math.min(elapsed / SEGMENT_MS, 1));

      const fromKey = SEQUENCE[segIdx.current % SEQUENCE.length];
      const toKey = SEQUENCE[(segIdx.current + 1) % SEQUENCE.length];
      const current = lerpShapes(SHAPES[fromKey], SHAPES[toKey], t);
      const d = buildPath(current, 38, 40, 40);

      mainRef.current?.setAttribute("d", d);

      if (elapsed >= SEGMENT_MS) {
        segIdx.current = (segIdx.current + 1) % SEQUENCE.length;
        segStart.current = now;
      }

      animRef.current = requestAnimationFrame(tick);
    };

    animRef.current = requestAnimationFrame(tick);
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [isAnimating]);

  // ── Render: animating state ───────────────────────────────────────────────
  if (isAnimating) {
    return (
      <div
        onClick={onClick}
        className="relative z-10 w-[64px] h-[64px] rounded-full flex items-center justify-center cursor-pointer hover:scale-105 transition-transform active:scale-95"
      >
        <svg
          viewBox="0 0 80 80"
          width={80}
          height={80}
          className="absolute fill-black dark:fill-white animate-[spin_12s_linear_infinite]"
          style={{
            top: -8,
            left: -8,
            overflow: "visible",
            zIndex: -1,
          }}
        >
          {/* Main morphing shape */}
          <path
            ref={mainRef}
            d={buildPath(SHAPES.scallop8, 38, 40, 40)}
          />
        </svg>
        <span className="material-symbols-outlined text-3xl text-white dark:text-black relative z-10">
          mic
        </span>
      </div>
    );
  }

  // ── Render: idle / connected state ────────────────────────────────────────
  return (
    <button
      onClick={onClick}
      className={`relative z-10 w-[64px] h-[64px] rounded-full flex items-center justify-center hover:scale-105 transition-transform active:scale-95 border-none ${
        isConnected
          ? "bg-red-600 text-white"
          : "bg-black dark:bg-white text-white dark:text-black"
      }`}
    >
      <span className="material-symbols-outlined text-3xl fill-current">
        {isConnected ? "mic_off" : "mic"}
      </span>
    </button>
  );
}
