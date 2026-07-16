/**
 * useNluAdapter.ts
 *
 * Adapts useNluVoice (Browser VAD + Deepgram + NLU WebSocket) into the exact
 * same interface that kiosk-view.tsx reads from LiveKit's useSessionContext.
 *
 * Shape matched:
 *   const { isConnected, start, end } = session
 *   const { state: agentState }       = useVoiceAssistant()
 *   const transcriptions               = useTranscriptions()
 *   const { messages }                 = useSessionMessages(session)
 *
 * All other LiveKit hooks (useTrackVolume, useTracks, useRoomContext) return
 * safe no-op stubs so kiosk-view.tsx compiles and runs unchanged.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useNluVoice, type NluResponse } from "@/hooks/useNluVoice";

// ── Types mirroring LiveKit shape ─────────────────────────────────────────────

/** Same states that LiveKit's useVoiceAssistant() emits. */
export type AgentState =
  | "disconnected"
  | "connecting"
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "pre-connect-buffering";

/** Minimal LiveKit "message" shape used by ChatTranscript. */
export interface NluMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
}

/** Minimal LiveKit "transcription" shape used by kiosk-view. */
export interface NluTranscription {
  text: string;
  isFinal: boolean;
  participantIdentity: string;
}

/** Returned by useNluAdapter — mirrors the LiveKit session + assistant hooks. */
export interface NluAdapter {
  // session object
  session: {
    isConnected: boolean;
    start: () => Promise<void>;
    end: () => void;
  };
  isConnected: boolean;
  start: () => Promise<void>;
  end: () => void;

  // useVoiceAssistant
  agentState: AgentState;

  // useSessionMessages
  messages: NluMessage[];

  // useTranscriptions
  transcriptions: NluTranscription[];

  // volume stubs (kiosk uses these for the pulse animation)
  maxVolume: number;

  // last NLU action payload (for future map/UI triggers)
  lastAction: NluResponse["action"] | null;
}

// ── Helper ────────────────────────────────────────────────────────────────────

let _msgId = 0;
const nextId = () => `nlu-${++_msgId}`;

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useNluAdapter(): NluAdapter {
  const [agentState, setAgentState] = useState<AgentState>("disconnected");
  const [messages, setMessages] = useState<NluMessage[]>([]);
  const [transcriptions, setTranscriptions] = useState<NluTranscription[]>([]);
  const [lastAction, setLastAction] = useState<NluResponse["action"] | null>(null);
  const [maxVolume, setMaxVolume] = useState(0);
  const speakingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Handle full NLU response
  const handleResponse = useCallback((response: NluResponse) => {
    setAgentState("speaking");
    if (response.reply_text) {
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "assistant",
          content: response.reply_text,
          timestamp: Date.now(),
        },
      ]);
    }
    if (response.action && Object.keys(response.action).length > 0) {
      setLastAction(response.action);
    }
  }, []);

  // Handle state changes from the NLU server
  const handleStateChange = useCallback((state: string) => {
    // Map NLU states to LiveKit agent states
    const map: Record<string, AgentState> = {
      idle: "idle",
      listening: "listening",
      thinking: "thinking",
      speaking: "speaking",
      connecting: "connecting",
      error: "idle",
    };
    const mapped = map[state] ?? "idle";
    setAgentState(mapped);

    // Animate maxVolume during speaking for the SiriGlow pulse
    if (mapped === "speaking") {
      let frame = 0;
      const animate = () => {
        const v = 0.3 + 0.5 * Math.abs(Math.sin(frame * 0.3));
        setMaxVolume(v);
        frame++;
        speakingTimerRef.current = setTimeout(animate, 80);
      };
      animate();
    } else {
      if (speakingTimerRef.current) clearTimeout(speakingTimerRef.current);
      setMaxVolume(mapped === "listening" ? 0.1 : 0);
    }
  }, []);

  const nlu = useNluVoice({
    nluServerUrl: process.env.NEXT_PUBLIC_NLU_SERVER_URL ?? "ws://localhost:8765/ws/voice",
    deepgramApiKey: process.env.NEXT_PUBLIC_DEEPGRAM_API_KEY,
    onResponse: handleResponse,
    onStateChange: handleStateChange,
  });

  // Mirror the NLU transcript into the transcriptions array so the
  // kiosk's staging text / scroll-to-bottom logic still works.
  useEffect(() => {
    if (!nlu.lastTranscript) return;
    const t: NluTranscription = {
      text: nlu.lastTranscript,
      isFinal: true,
      participantIdentity: "user",
    };
    setTranscriptions((prev) => [...prev.slice(-19), t]);
    // Also append the user utterance to messages
    setMessages((prev) => [
      ...prev,
      {
        id: nextId(),
        role: "user",
        content: nlu.lastTranscript,
        timestamp: Date.now(),
      },
    ]);
  }, [nlu.lastTranscript]);

  // Cleanup volume animation on unmount
  useEffect(() => {
    return () => {
      if (speakingTimerRef.current) clearTimeout(speakingTimerRef.current);
    };
  }, []);

  const start = useCallback(async () => {
    setAgentState("connecting");
    try {
      await nlu.start();
    } catch (e) {
      setAgentState("disconnected");
      throw e;
    }
  }, [nlu]);

  const end = useCallback(() => {
    nlu.stop();
    setAgentState("disconnected");
    setMessages([]);
    setTranscriptions([]);
  }, [nlu]);

  const isConnected = nlu.isActive;

  const session = { isConnected, start, end };

  return {
    session,
    isConnected,
    start,
    end,
    agentState,
    messages,
    transcriptions,
    maxVolume,
    lastAction,
  };
}
