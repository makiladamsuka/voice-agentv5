"use client";

import { useEffect, useRef } from "react";
import { motion } from "motion/react";
import { M3_POP } from "@/components/ui/PopButton";

// ─── Shape generation (M3 Polar Math) ──────────────────────────────────────────
const POINTS = 120;

function getPolarShape(numPetals: number, depth: number, phase: number = 0): number[] {
  const pts = [];
  for (let i = 0; i < POINTS; i++) {
    const theta = (i / POINTS) * Math.PI * 2;
    const r = 1 - depth + depth * Math.cos(numPetals * theta + phase);
    pts.push(r * Math.cos(theta), r * Math.sin(theta));
  }
  return pts;
}

const SHAPES: Record<string, number[]> = {
  scallop8: getPolarShape(8, 0.08),
  cushion4: getPolarShape(4, 0.12, Math.PI),
  scallop6: getPolarShape(6, 0.12),
  clover4: getPolarShape(4, 0.22),
  scallop8_deep: getPolarShape(8, 0.14),
  pentagon: getPolarShape(5, 0.12, 0),
  hexagon: getPolarShape(6, 0.12, 0),
  smooth4: getPolarShape(4, 0.05, Math.PI),
  smooth6: getPolarShape(6, 0.04, 0),
  smooth8: getPolarShape(8, 0.03, 0),
};

const SEQUENCE = ["smooth4", "smooth6", "smooth8", "smooth6"];

function easeInOut(t: number) {
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

interface GeminiMorphButtonProps {
  isAnimating: boolean;
  isConnected: boolean;
  onClick: () => void;
  size?: number;
  volume?: number;
}

export function GeminiMorphButton({
  isAnimating,
  onClick,
  size = 72,
  volume = 0,
}: GeminiMorphButtonProps) {
  const mainRef = useRef<SVGPathElement | null>(null);
  const animRef = useRef<number | null>(null);
  const segIdx = useRef(0);
  const segStart = useRef(0);
  const SEGMENT_MS = 4000;

  useEffect(() => {
    if (!isAnimating) {
      if (animRef.current) cancelAnimationFrame(animRef.current);
      return;
    }

    segIdx.current = 0;
    segStart.current = 0;

    // Set initial path immediately to prevent blank SVG flash during mount
    const initialKey = SEQUENCE[0];
    const initialD = buildPath(SHAPES[initialKey], 38, 40, 40);
    if (mainRef.current) {
      mainRef.current.setAttribute("d", initialD);
    }

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

  if (isAnimating) {
    return (
      <motion.div
        key="morph"
        onClick={onClick}
        initial={{ scale: 0.72 }}
        animate={{ scale: 1 + volume * 0.3 }}
        whileTap={{ scale: 0.92 }}
        transition={M3_POP}
        className="relative z-10 rounded-full flex items-center justify-center cursor-pointer"
        style={{ width: size, height: size }}
      >
        <svg
          viewBox="0 0 80 80"
          width={size + 16}
          height={size + 16}
          className="absolute fill-black dark:fill-white animate-[spin_12s_linear_infinite]"
          style={{
            top: -8,
            left: -8,
            overflow: "visible",
            zIndex: -1,
          }}
        >
          <path ref={mainRef} />
        </svg>
        <span className="material-symbols-outlined text-3xl text-white dark:text-black relative z-10">
          mic
        </span>
      </motion.div>
    );
  }

  return (
    <motion.button
      key="idle"
      type="button"
      onClick={onClick}
      initial={{ scale: 1.18 }}
      animate={{ scale: 1 }}
      whileTap={{ scale: 0.92 }}
      transition={M3_POP}
      className="relative z-10 rounded-full flex items-center justify-center border-none shadow-sm bg-black dark:bg-white text-white dark:text-black"
      style={{ width: size, height: size }}
    >
      <span className="material-symbols-outlined text-3xl fill-current">
        mic
      </span>
    </motion.button>
  );
}
