"use client";

import { useEffect, useMemo, useState } from "react";
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

export function EmojiStage({
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
  }, [noFaceSec, faceDetected]);

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
    <div className="flex flex-col items-center justify-center my-4 select-none">
      <div className="relative flex items-center justify-center w-40 h-40 md:w-48 md:h-48">
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
            className={`text-[100px] md:text-[130px] leading-none anim-emoji-${cue.animation}`}
            role="img"
            aria-label={cue.label}
          >
            {cue.emoji}
          </span>
        )}
      </div>
    </div>
  );
}
