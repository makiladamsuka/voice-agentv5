"use client";

import { motion } from "motion/react";
import { M3_POP } from "@/components/ui/PopButton";

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
  if (isAnimating) {
    return (
      <motion.div
        key="morph"
        onClick={onClick}
        initial={{ scale: 0.72 }}
        // React to volume, adding a subtle scale boost based on audio level
        animate={{ scale: 1 + volume * 0.3 }}
        whileTap={{ scale: 0.92 }}
        transition={M3_POP}
        className="relative z-10 rounded-full flex items-center justify-center cursor-pointer bg-black dark:bg-white"
        style={{ width: size, height: size }}
      >
        {/* Simple spinning outline instead of complex JS morphing SVG */}
        <motion.div
          className="absolute inset-[-6px] rounded-full border-2 border-dashed border-black/20 dark:border-white/20 pointer-events-none"
          animate={{ rotate: 360 }}
          transition={{ duration: 8, repeat: Infinity, ease: "linear" }}
        />
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
