"use client";

import { useEffect, useState } from "react";
import { RoomAudioRenderer } from "@livekit/components-react";
import { KioskAudioBoost } from "@/components/app/kiosk-audio-boost";

/** Silence any stray <audio> tags (LiveKit) when Pi plays TTS on backend speakers. */
function BrowserAudioSilencer() {
  useEffect(() => {
    const mute = () => {
      document.querySelectorAll("audio").forEach((el) => {
        el.muted = true;
        el.volume = 0;
      });
    };
    mute();
    const observer = new MutationObserver(mute);
    observer.observe(document.body, { childList: true, subtree: true });
    const timer = setInterval(mute, 500);
    return () => {
      observer.disconnect();
      clearInterval(timer);
    };
  }, []);
  return null;
}

/**
 * Runtime check of /api/voice-config — works without rebuilding frontend when
 * NEXT_PUBLIC_LOCAL_SPEAKER was not set at build time.
 */
export function VoiceAudioOutput() {
  const [localSpeaker, setLocalSpeaker] = useState<boolean | null>(null);

  useEffect(() => {
    const envLocal =
      process.env.NEXT_PUBLIC_LOCAL_SPEAKER === "1" ||
      process.env.NEXT_PUBLIC_LOCAL_SPEAKER === "true";

    fetch("/api/voice-config", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.localSpeaker === "boolean") {
          setLocalSpeaker(data.localSpeaker);
        } else {
          setLocalSpeaker(envLocal);
        }
      })
      .catch(() => setLocalSpeaker(envLocal));
  }, []);

  if (localSpeaker === null) {
    return null;
  }

  if (localSpeaker) {
    return <BrowserAudioSilencer />;
  }

  return (
    <>
      <RoomAudioRenderer />
      <KioskAudioBoost />
    </>
  );
}
