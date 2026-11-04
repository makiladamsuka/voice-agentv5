"use client";

import { memo, useEffect, useMemo, useState } from "react";
import { getEmojiCue } from "@/lib/emoji-cues";

interface EmojiStageProps {
  transcript: string;
  convState: string;
  emotion?: string;
  faceCount?: number;
  faceDetected?: boolean;
  faceArea?: number;
  noFaceSec?: number;
}

export const EmojiStage = memo(function EmojiStage({
  transcript,
  convState,
  emotion = "idle",
  faceCount = 0,
  faceDetected = false,
  faceArea = 0,
  noFaceSec = 0,
}: EmojiStageProps) {
  // Baseline idle cycle tick (advances every 18 seconds)
  const [idleCycleTick, setIdleCycleTick] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setIdleCycleTick((t) => t + 1);
    }, 18_000); // 18 seconds gentle rotation between ✨, 🤖, and 👀
    return () => clearInterval(interval);
  }, []);

  // Local fallback timer for no-face idle if backend status is offline
  const [localNoFaceSec, setLocalNoFaceSec] = useState(0);
  useEffect(() => {
    // If backend provides live noFaceSec, let it take precedence
    if (noFaceSec > 0 || faceDetected) {
      setLocalNoFaceSec(0);
      return;
    }
    const interval = setInterval(() => {
      setLocalNoFaceSec((s) => s + 1);
    }, 1000);
    return () => clearInterval(interval);
  }, [faceDetected]);

  const effectiveNoFaceSec = noFaceSec > 0 ? noFaceSec : localNoFaceSec;

  const cue = useMemo(
    () =>
      getEmojiCue(transcript, convState, emotion, faceCount, {
        faceDetected,
        faceArea,
        noFaceSec: effectiveNoFaceSec,
        idleCycleTick,
      }),
    [transcript, convState, emotion, faceCount, faceDetected, faceArea, effectiveNoFaceSec, idleCycleTick]
  );

  const [failedCodePoints, setFailedCodePoints] = useState<Record<string, boolean>>({});
  const isFailed = Boolean(failedCodePoints[cue.codePoint]);

  // WhatsApp / Google Noto 3D Animated Emoji URL
  const animatedUrl = `https://fonts.gstatic.com/s/e/notoemoji/latest/${cue.codePoint}/512.webp`;

  return (
    <div className="flex flex-col items-center justify-center my-1 select-none shrink-0">
      <div className="relative flex items-center justify-center w-24 h-24 sm:w-32 sm:h-32 md:w-40 md:h-40">
        {!isFailed ? (
          <img
            key={cue.codePoint}
            src={animatedUrl}
            alt={cue.label}
            onError={() => setFailedCodePoints((prev) => ({ ...prev, [cue.codePoint]: true }))}
            className="w-full h-full object-contain drop-shadow-xl"
            loading="eager"
          />
        ) : (
          <span
            className={`text-[64px] sm:text-[80px] md:text-[100px] leading-none anim-emoji-${cue.animation}`}
            role="img"
            aria-label={cue.label}
          >
            {cue.emoji}
          </span>
        )}
      </div>
    </div>
  );
});
