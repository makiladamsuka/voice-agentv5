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
  style,
  ...props
}: PopButtonProps) {
  return (
    <motion.button
      whileTap={whileTap ?? { scale: 0.92 }}
      transition={transition ?? M3_POP}
      style={{
        // Kill 300ms tap delay and grey highlight on touchscreen
        touchAction: "manipulation",
        WebkitTapHighlightColor: "transparent",
        // Ensure minimum 48px touch target for ergonomic kiosk touch
        minHeight: "var(--touch-target, 48px)",
        minWidth: "var(--touch-target, 48px)",
        // Hardware-accelerated transforms for instant touch feedback
        willChange: "transform",
        ...style,
      }}
      {...props}
    >
      {children}
    </motion.button>
  );
}
