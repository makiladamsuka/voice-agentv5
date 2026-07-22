import React from 'react';
import { motion, AnimatePresence } from 'motion/react';

interface SiriGlowProps {
  active: boolean;
}

export const SiriGlow: React.FC<SiriGlowProps> = ({ active }) => {
  return (
    <AnimatePresence>
      {active && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.4, ease: "easeInOut" }}
          // We use inset-0 so it sits EXACTLY inside the parent boundary.
          // z-50 ensures it stays on top of the content. 
          // p-[3px] defines the thickness of the glow border.
          className="absolute inset-0 z-50 rounded-[32px] pointer-events-none p-[3px]"
          style={{
            // This mask technique hides the inner content box, revealing ONLY the 2px padding (border).
            WebkitMask: 'linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)',
            WebkitMaskComposite: 'xor',
            maskComposite: 'exclude',
          }}
        >
          {/* 
            The rotating gradient. It is intentionally oversized (250%) so it covers the corners 
            even while spinning. We add a tiny blur to soften the harsh CSS gradient edges.
          */}
          <div 
            className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[250%] h-[250%] blur-[8px] opacity-90 animate-[spin_4s_linear_infinite]"
            style={{
              background: 'conic-gradient(from 0deg, transparent 15%, #ff2a85 30%, #8a2be2 45%, #4169e1 60%, #ffd700 75%, transparent 90%)',
            }}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
};
