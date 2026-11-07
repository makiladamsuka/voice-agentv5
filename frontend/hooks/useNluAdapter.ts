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
import { useFaceGreeting, type RobotLiveStatus } from "@/hooks/useFaceGreeting";

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
  
  // method to simulate user voice input (e.g. from UI buttons)
  sendSimulatedVoice: (text: string) => void;

  // method to send a screen-touch event (triggers robot look-down)
  sendScreenEvent: (x_norm: number) => void;

  // Live robot presence & TFT eye emotion status
  robotStatus: RobotLiveStatus;
}

// ── Helper ────────────────────────────────────────────────────────────────────

let _msgId = 0;
const nextId = () => `nlu-${++_msgId}`;

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface UseNluAdapterOptions {
  autoGreetEnabled?: boolean;
}

export function useNluAdapter(options?: UseNluAdapterOptions): NluAdapter {
  const [agentState, setAgentState] = useState<AgentState>("disconnected");
  const [messages, setMessages] = useState<NluMessage[]>([]);
  const [transcriptions, setTranscriptions] = useState<NluTranscription[]>([]);
  const [lastAction, setLastAction] = useState<NluResponse["action"] | null>(null);
  const [maxVolume, setMaxVolume] = useState(0);
  const [robotStatus, setRobotStatus] = useState<RobotLiveStatus>({
    emotion: "idle",
    faceDetected: false,
    faceCount: 0,
    faceArea: 0,
    noFaceSec: 0,
  });

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

    // A new user turn has started — clear stale buttons from the previous response
    // so they don't linger while the agent is thinking.
    if (mapped === "thinking") {
      setLastAction((prev) => {
        if (!prev || !prev.suggested_buttons) return prev;
        const { suggested_buttons: _, ...rest } = prev as Record<string, unknown>;
        return Object.keys(rest).length > 0 ? (rest as typeof prev) : null;
      });
    }

    // Volume is only consumed while listening (a static level). There is no
    // per-frame animation here: updating React state every few ms re-rendered
    // the entire kiosk UI and was costly on the Pi.
    setMaxVolume(mapped === "listening" ? 0.1 : 0);
  }, []);

  const nlu = useNluVoice({
    nluServerUrl: process.env.NEXT_PUBLIC_NLU_SERVER_URL ?? "ws://localhost:8765/ws/voice",
    deepgramApiKey: process.env.NEXT_PUBLIC_DEEPGRAM_API_KEY,
    playChimes: true,
    onResponse: handleResponse,
    onStateChange: handleStateChange,
    onVolumeChange: (vol: number) => {
      // Only update if we are listening (speaking is handled by the fake sine wave)
      if (agentState === "listening") {
        setMaxVolume(0.1 + vol * 0.9);
      }
    }
  });

  const isBusy = useCallback(
    () => agentState === "speaking" || agentState === "thinking",
    [agentState]
  );

  const { sendScreenTouch } = useFaceGreeting({
    nluServerUrl: process.env.NEXT_PUBLIC_NLU_SERVER_URL ?? "ws://localhost:8765",
    enabled: options?.autoGreetEnabled ?? true,
    isBusy,
    onGreetingStart: (text: string) => {
      setAgentState("speaking");
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "assistant",
          content: text,
          timestamp: Date.now(),
        },
      ]);
    },
    onGreetingEnd: () => {
      setAgentState("idle");
    },
    onStatusUpdate: (status) => {
      setRobotStatus((prev) => {
        const isFarPrev = prev.faceArea > 0 && prev.faceArea < 0.035;
        const isFarNew = status.faceArea > 0 && status.faceArea < 0.035;
        const isSleepyPrev = prev.noFaceSec >= 150;
        const isSleepyNew = status.noFaceSec >= 150;
        if (
          prev.emotion === status.emotion &&
          prev.faceDetected === status.faceDetected &&
          prev.faceCount === status.faceCount &&
          isFarPrev === isFarNew &&
          isSleepyPrev === isSleepyNew &&
          prev.handGesture === status.handGesture
        ) {
          return prev;
        }
        return status;
      });
    },
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

  // ── Auto-disconnect: no real speech detected ───────────────────────────────
  // Exhibition-safe approach: we don't count raw silence seconds (background
  // noise keeps Deepgram's VAD firing). Instead we:
  //   1. Reset a wall-clock "last activity" timestamp any time the user sends
  //      a real transcript OR the agent responds (meaningful interaction).
  //   2. If IDLE_DISCONNECT_MS passes with no meaningful activity while
  //      connected, we auto-disconnect.
  //
  // IDLE_DISCONNECT_MS is long (90s) to tolerate exhibition background noise
  // that briefly triggers Deepgram VAD but never resolves to a real transcript.
  const IDLE_DISCONNECT_MS = 90_000; // 90 s — tuned for noisy environments
  const lastActivityRef = useRef<number>(Date.now());
  const idleTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Bump activity timestamp on any real user transcript
  useEffect(() => {
    if (nlu.lastTranscript) {
      lastActivityRef.current = Date.now();
    }
  }, [nlu.lastTranscript]);

  // Bump activity timestamp when the agent replies (agent spoke = interaction)
  useEffect(() => {
    if (agentState === "speaking") {
      lastActivityRef.current = Date.now();
    }
  }, [agentState]);

  // Start/stop the idle watchdog whenever connection state changes
  const endRef = useRef<() => void>(() => {});
  useEffect(() => {
    // We'll fill endRef after `end` is defined below — capture via ref to
    // avoid a dependency cycle.
    const isConn = nlu.isActive;
    if (!isConn) {
      if (idleTimerRef.current) {
        clearInterval(idleTimerRef.current);
        idleTimerRef.current = null;
      }
      return;
    }

    // Reset activity clock when we first connect
    lastActivityRef.current = Date.now();

    idleTimerRef.current = setInterval(() => {
      const idleMs = Date.now() - lastActivityRef.current;
      if (idleMs >= IDLE_DISCONNECT_MS) {
        console.log(
          `[NluAdapter] Auto-disconnect: no real speech for ${Math.round(idleMs / 1000)}s`
        );
        endRef.current();
      }
    }, 5_000); // check every 5 s

    return () => {
      if (idleTimerRef.current) {
        clearInterval(idleTimerRef.current);
        idleTimerRef.current = null;
      }
    };
  }, [nlu.isActive]);

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

  // Keep endRef in sync so the idle watchdog can call it without a dep cycle
  endRef.current = end;

  // Send screen event through persistent /ws/greet socket and bump activity clock
  const sendScreenEvent = useCallback(
    (x_norm: number) => {
      lastActivityRef.current = Date.now();
      const clientX =
        typeof window !== "undefined" && window.innerWidth > 0
          ? ((x_norm + 1) / 2) * window.innerWidth
          : undefined;
      sendScreenTouch(clientX, "touch");
    },
    [sendScreenTouch]
  );

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
    sendSimulatedVoice: nlu.sendSimulatedVoice,
    sendScreenEvent,
    robotStatus,
  };
}
