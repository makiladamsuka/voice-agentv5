"use client";

import { motion, type HTMLMotionProps } from "motion/react";

/** Material 3 expressive — snappy overshoot pop */
export const M3_POP = {
  type: "spring" as const,
  stiffness: 700,
  damping: 18,
  mass: 0.4,
};

type PopButtonProps = HTMLMotionProps<"button">;

export function PopButton({
  children,
  whileTap,
  transition,
  ...props
}: PopButtonProps) {
  return (
    <motion.button
      whileTap={whileTap ?? { scale: 0.92 }}
      transition={transition ?? M3_POP}
      {...props}
    >
      {children}
    </motion.button>
  );
}
