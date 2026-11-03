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
        initial={{ scale: 0.9 }}
        animate={{ scale: 1 + volume * 0.25 }}
        whileTap={{ scale: 0.94 }}
        transition={M3_POP}
        className="relative z-10 rounded-full flex items-center justify-center cursor-pointer bg-black dark:bg-white shadow-lg active:scale-95"
        style={{ width: size, height: size }}
      >
        <span className="material-symbols-outlined text-4xl text-white dark:text-black relative z-10">
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
      initial={{ scale: 1.05 }}
      animate={{ scale: 1 }}
      whileTap={{ scale: 0.94 }}
      transition={M3_POP}
      className="relative z-10 rounded-full flex items-center justify-center border-none shadow-md bg-black dark:bg-white text-white dark:text-black active:scale-95"
      style={{ width: size, height: size }}
    >
      <span className="material-symbols-outlined text-4xl fill-current">
        mic
      </span>
    </motion.button>
  );
}
