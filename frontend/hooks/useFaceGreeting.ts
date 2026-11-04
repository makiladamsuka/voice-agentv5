/**
 * useFaceGreeting — Autonomous face-detection greeting hook.
 *
 * Connects to the backend /ws/greet WebSocket endpoint.
 * When the backend detects a new face, it pushes a greeting text + audio URL.
 * This hook plays that audio through the browser's speakers immediately,
 * completely independently of the voice conversation session.
 *
 * Features:
 * - Auto-reconnects on disconnect (exponential backoff, max 10s)
 * - Does NOT play greeting while agent is already speaking (voice state guard)
 * - Falls back to /api/tts if no pre-generated audio_url is provided
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface RobotLiveStatus {
  emotion: string;
  faceDetected: boolean;
  faceCount: number;
  faceArea: number;
  noFaceSec: number;
}

interface FaceGreetingOptions {
  /** NLU server WebSocket base URL, e.g. "ws://127.0.0.1:8765" */
  nluServerUrl?: string;
  /** Whether face greeting is enabled */
  enabled?: boolean;
  /** Ref or state indicating whether the agent is currently speaking/thinking */
  isBusy?: () => boolean;
  /** Called when greeting playback starts with the spoken text */
  onGreetingStart?: (text: string) => void;
  /** Called when greeting playback ends */
  onGreetingEnd?: () => void;
  /** Called when live presence / emotion status arrives from robot */
  onStatusUpdate?: (status: RobotLiveStatus) => void;
}

export function useFaceGreeting({
  nluServerUrl = "ws://127.0.0.1:8765",
  enabled = true,
  isBusy,
  onGreetingStart,
  onGreetingEnd,
  onStatusUpdate,
}: FaceGreetingOptions = {}) {
  const [isGreeting, setIsGreeting] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelayRef = useRef(1000);
  const mountedRef = useRef(true);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const currentAudioRef = useRef<AudioBufferSourceNode | null>(null);

  const getAudioCtx = useCallback(() => {
    if (!audioCtxRef.current || audioCtxRef.current.state === "closed") {
      const Ctx = window.AudioContext || (window as any).webkitAudioContext;
      audioCtxRef.current = new Ctx({ sampleRate: 48000 });
    }
    return audioCtxRef.current;
  }, []);

  const playGreeting = useCallback(
    async (text: string, audioUrl: string | null) => {
      if (!enabled) return;
      // Don't interrupt an ongoing agent response
      if (isBusy?.()) {
        console.log("[FaceGreeting] Skipped — agent busy.");
        return;
      }

      // Stop any previous greeting
      try {
        currentAudioRef.current?.stop();
      } catch (_) {}
      currentAudioRef.current = null;

      const backendHost =
        typeof window !== "undefined" ? window.location.hostname : "localhost";

      const resolvedUrl = audioUrl
        ? `http://${backendHost}:8080${audioUrl}`
        : `/api/tts?text=${encodeURIComponent(text)}`;

      try {
        const ctx = getAudioCtx();
        if (ctx.state === "suspended") await ctx.resume();

        const response = await fetch(resolvedUrl);
        if (!response.ok) throw new Error(`Fetch failed: ${response.status}`);
        const arrayBuffer = await response.arrayBuffer();
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);

        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        const gainNode = ctx.createGain();
        const masterVol = typeof (window as any)._masterVolume === "number" ? (window as any)._masterVolume : 1.0;
        gainNode.gain.setValueAtTime(masterVol, ctx.currentTime);
        source.connect(gainNode);
        gainNode.connect(ctx.destination);
        currentAudioRef.current = source;

        // Notify parent: greeting started with text
        setIsGreeting(true);
        onGreetingStart?.(text);

        source.onended = () => {
          setIsGreeting(false);
          onGreetingEnd?.();
          currentAudioRef.current = null;
        };

        source.start(0);
        console.log(`[FaceGreeting] 🎙️ Playing: "${text}"`);
      } catch (e) {
        console.warn("[FaceGreeting] Web Audio failed, trying HTML5 Audio fallback:", e);
        try {
          const audio = new Audio(resolvedUrl);
          const masterVol = typeof (window as any)._masterVolume === "number" ? (window as any)._masterVolume : 1.0;
          audio.volume = masterVol;
          currentAudioRef.current = audio as any;
          audio.onplay = () => {
            setIsGreeting(true);
            onGreetingStart?.(text);
          };
          audio.onended = () => {
            setIsGreeting(false);
            onGreetingEnd?.();
            currentAudioRef.current = null;
          };
          audio.onerror = () => {
            setIsGreeting(false);
            onGreetingEnd?.();
            currentAudioRef.current = null;
          };
          await audio.play();
        } catch (e2) {
          console.error("[FaceGreeting] Audio playback failed:", e2);
          setIsGreeting(false);
          onGreetingEnd?.();
        }
      }
    },
    [enabled, isBusy, getAudioCtx, onGreetingStart, onGreetingEnd]
  );

  const connect = useCallback(() => {
    if (!mountedRef.current || !enabled) return;

    const backendHost =
      typeof window !== "undefined" ? window.location.hostname : "localhost";
    const url = nluServerUrl
      .replace("localhost", backendHost)
      .replace("127.0.0.1", backendHost)
      .replace(/^ws/, "ws") // keep ws/wss
      .replace(/\/ws\/voice$/, "") // strip voice path if present
      .replace(/\/$/, "") + "/ws/greet";

    console.log(`[FaceGreeting] Connecting to ${url}`);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("[FaceGreeting] Connected to greeting endpoint.");
      reconnectDelayRef.current = 1000; // reset backoff
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "greeting") {
          void playGreeting(msg.text, msg.audio_url ?? null);
        } else if (msg.type === "robot_status") {
          onStatusUpdate?.({
            emotion: String(msg.emotion || "idle"),
            faceDetected: Boolean(msg.face_detected),
            faceCount: Number(msg.face_count || 0),
            faceArea: Number(msg.face_area || 0),
            noFaceSec: Number(msg.no_face_sec || 0),
          });
        }
      } catch (_) {}
    };

    ws.onerror = () => {
      console.warn("[FaceGreeting] WebSocket error.");
    };

    ws.onclose = () => {
      console.log("[FaceGreeting] Disconnected. Reconnecting...");
      if (!mountedRef.current || !enabled) return;
      reconnectTimerRef.current = setTimeout(() => {
        reconnectDelayRef.current = Math.min(
          reconnectDelayRef.current * 2,
          10_000
        );
        connect();
      }, reconnectDelayRef.current);
    };
  }, [nluServerUrl, enabled, playGreeting]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      try {
        wsRef.current?.close();
      } catch (_) {}
      try {
        currentAudioRef.current?.stop();
      } catch (_) {}
      setIsGreeting(false);
      return;
    }
    // Small delay so the NLU server has time to start before we connect
    const t = setTimeout(connect, 2000);
    return () => {
      mountedRef.current = false;
      clearTimeout(t);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      try {
        wsRef.current?.close();
      } catch (_) {}
    };
  }, [enabled, connect]);

  return { isGreeting };
}
