"use client";

import {
  useSessionContext,
  useTranscriptions,
  useVoiceAssistant,
} from "@livekit/components-react";
import { Mic, MicOff, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  sessionStartOptions,
  useVoiceConfig,
} from "@/hooks/use-voice-config";

/** Minimal voice UI for Pi kiosk — no 3D maps, polling, or rAF morph animations. */
export function VoiceLiteView() {
  const session = useSessionContext();
  const { isConnected, start, end } = session;
  const { state: agentState } = useVoiceAssistant();
  const transcriptions = useTranscriptions();

  const voiceConfig = useVoiceConfig();
  const [isConnecting, setIsConnecting] = useState(false);
  const [status, setStatus] = useState("Tap to start voice");

  const latestText =
    transcriptions[transcriptions.length - 1]?.text?.trim() ?? "";

  useEffect(() => {
    if (isConnecting) {
      setStatus("Connecting…");
      return;
    }
    if (!isConnected) {
      setStatus("Tap to start voice");
      return;
    }
    if (agentState === "thinking") {
      setStatus("Thinking…");
      return;
    }
    if (agentState === "speaking") {
      setStatus("Speaking…");
      return;
    }
    setStatus("Listening…");
  }, [isConnected, isConnecting, agentState]);

  const handleMicClick = useCallback(async () => {
    if (isConnected) {
      end();
      return;
    }
    setIsConnecting(true);
    try {
      const timeout = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("Connection timeout")), 30000),
      );
      await Promise.race([
        start(sessionStartOptions(voiceConfig?.localMic)),
        timeout,
      ]);
    } catch (e) {
      console.error("Agent connection failed:", e);
      setStatus("Connection failed — tap to retry");
    } finally {
      setIsConnecting(false);
    }
  }, [isConnected, start, end, voiceConfig?.localMic]);

  return (
    <div className="flex h-svh w-full flex-col items-center justify-center gap-8 bg-[#f0f4f9] px-6 dark:bg-[#0f1419]">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-bold text-on-surface">Campus Voice</h1>
        <p className="mt-2 text-lg text-on-surface-variant">{status}</p>
        {latestText ? (
          <p className="mt-4 text-base text-on-surface/80">{latestText}</p>
        ) : null}
      </div>

      <button
        type="button"
        onClick={handleMicClick}
        disabled={isConnecting}
        aria-label={isConnected ? "End voice session" : "Start voice session"}
        className={`flex h-28 w-28 items-center justify-center rounded-full shadow-lg transition-colors ${
          isConnected
            ? "bg-primary text-on-primary"
            : "bg-[#1a73e8] text-white"
        } disabled:opacity-70`}
      >
        {isConnecting ? (
          <Loader2 className="h-10 w-10 animate-spin" />
        ) : isConnected ? (
          <MicOff className="h-10 w-10" />
        ) : (
          <Mic className="h-10 w-10" />
        )}
      </button>

      <p className="text-sm text-on-surface-variant/70">
        Full kiosk UI:{" "}
        <a href="/" className="underline">
          /
        </a>
      </p>
    </div>
  );
}
