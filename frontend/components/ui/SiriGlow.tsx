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
          initial={{ opacity: 0, scale: 0.98 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.98 }}
          transition={{ duration: 0.3, ease: "easeInOut" }}
          className="absolute inset-0 z-50 rounded-[32px] pointer-events-none border-[3px] border-[#8a2be2]"
          style={{
            boxShadow: '0 0 20px rgba(138, 43, 226, 0.5), inset 0 0 20px rgba(138, 43, 226, 0.5)'
          }}
        >
          {/* Simple animated pulsing border without heavy SVG masks or blurs */}
          <motion.div 
            className="absolute inset-0 rounded-[29px]"
            animate={{ 
              boxShadow: [
                '0 0 10px rgba(255, 42, 133, 0.2), inset 0 0 10px rgba(255, 42, 133, 0.2)',
                '0 0 30px rgba(65, 105, 225, 0.6), inset 0 0 30px rgba(65, 105, 225, 0.6)',
                '0 0 10px rgba(255, 42, 133, 0.2), inset 0 0 10px rgba(255, 42, 133, 0.2)'
              ]
            }}
            transition={{
              duration: 2,
              repeat: Infinity,
              ease: "easeInOut"
            }}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
};

