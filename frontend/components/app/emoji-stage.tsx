"use client";

import { useMemo, useState } from "react";
import { getEmojiCue } from "@/lib/emoji-cues";

interface EmojiStageProps {
  transcript: string;
  convState: string;
  emotion?: string;
  faceCount?: number;
}

export function EmojiStage({
  transcript,
  convState,
  emotion = "idle",
  faceCount = 0,
}: EmojiStageProps) {
  const cue = useMemo(
    () => getEmojiCue(transcript, convState, emotion, faceCount),
    [transcript, convState, emotion, faceCount]
  );

  const [imgError, setImgError] = useState(false);

  // WhatsApp / Google Noto 3D Animated Emoji URL
  const animatedUrl = `https://fonts.gstatic.com/s/e/notoemoji/latest/${cue.codePoint}/512.webp`;

  return (
    <div className="flex flex-col items-center justify-center my-4 select-none">
      <div className="relative flex items-center justify-center w-52 h-52 md:w-64 md:h-64">
        {!imgError ? (
          <img
            key={cue.codePoint}
            src={animatedUrl}
            alt={cue.label}
            onError={() => setImgError(true)}
            className="w-full h-full object-contain drop-shadow-xl"
            loading="eager"
          />
        ) : (
          <span
            className={`text-[140px] md:text-[180px] leading-none anim-emoji-${cue.animation}`}
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
