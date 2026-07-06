"use client";

import { useEffect, useState } from "react";

export type VoiceConfig = {
  localSpeaker: boolean;
  localMic: boolean;
};

export function useVoiceConfig(): VoiceConfig | null {
  const [config, setConfig] = useState<VoiceConfig | null>(null);

  useEffect(() => {
    const envSpeaker =
      process.env.NEXT_PUBLIC_LOCAL_SPEAKER === "1" ||
      process.env.NEXT_PUBLIC_LOCAL_SPEAKER === "true";
    const envMic =
      process.env.NEXT_PUBLIC_LOCAL_MIC === "1" ||
      process.env.NEXT_PUBLIC_LOCAL_MIC === "true";

    fetch("/api/voice-config", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.localSpeaker === "boolean") {
          setConfig({
            localSpeaker: data.localSpeaker,
            localMic: Boolean(data.localMic),
          });
        } else {
          setConfig({ localSpeaker: envSpeaker, localMic: envMic });
        }
      })
      .catch(() => setConfig({ localSpeaker: envSpeaker, localMic: envMic }));
  }, []);

  return config;
}

export function sessionStartOptions(localMic: boolean | undefined) {
  if (localMic) {
    return { tracks: { microphone: { enabled: false as const } } };
  }
  return undefined;
}
