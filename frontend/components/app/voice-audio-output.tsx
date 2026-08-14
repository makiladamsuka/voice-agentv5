"use client";

import { useEffect } from "react";

/** Mutes any stray <audio> tags injected by third-party libs. Pi backend plays TTS on speakers. */
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

/** NLU kiosk: backend handles TTS audio — browser audio is always silenced. */
export function VoiceAudioOutput() {
  return <BrowserAudioSilencer />;
}
