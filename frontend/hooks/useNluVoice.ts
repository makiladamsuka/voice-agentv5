/**
 * useNluVoice.ts
 *
 * Drop-in replacement for LiveKit's useVoiceAssistant / SessionProvider.
 *
 * This hook handles the complete NLU voice pipeline entirely on the browser:
 *   1. Browser VAD (@ricky0123/vad-web) detects when the user starts/stops speaking.
 *   2. Raw audio is streamed directly from the browser to Deepgram STT (no Pi CPU used).
 *   3. The final transcript is sent over a local WebSocket to the Python NLU server.
 *   4. The Python server returns a JSON response with the reply text and a cached audio URL.
 *   5. The browser plays the cached MP3, or falls back to Deepgram TTS for dynamic replies.
 *   6. Barge-in: if VAD detects speech while audio is playing, the audio is instantly paused.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

export type NluVoiceState =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "connecting"
  | "error";

export interface NluResponse {
  reply_text: string;
  audio_url: string | null;
  action: Record<string, string> | null;
}

interface UseNluVoiceOptions {
  /** Local Python NLU WebSocket server URL. Default: ws://localhost:8765/ws/voice */
  nluServerUrl?: string;
  /** Deepgram API key for STT. Reads NEXT_PUBLIC_DEEPGRAM_API_KEY env var if not provided. */
  deepgramApiKey?: string;
  /** Called when the NLU server sends a complete response. */
  onResponse?: (response: NluResponse) => void;
  /** Called when the voice state changes (for UI feedback). */
  onStateChange?: (state: NluVoiceState) => void;
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useNluVoice({
  nluServerUrl = "ws://localhost:8765/ws/voice",
  deepgramApiKey,
  onResponse,
  onStateChange,
}: UseNluVoiceOptions = {}) {
  const [state, setState] = useState<NluVoiceState>("idle");
  const [isActive, setIsActive] = useState(false);
  const [lastTranscript, setLastTranscript] = useState("");

  // Refs (mutable, no re-render)
  const nluWs = useRef<WebSocket | null>(null);
  const dgWs = useRef<WebSocket | null>(null);
  const vadRef = useRef<any>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const stateRef = useRef<NluVoiceState>("idle");

  const apiKey =
    deepgramApiKey ?? process.env.NEXT_PUBLIC_DEEPGRAM_API_KEY ?? "";

  // Keep stateRef in sync so callbacks can read state without stale closures
  const setVoiceState = useCallback(
    (newState: NluVoiceState) => {
      stateRef.current = newState;
      setState(newState);
      onStateChange?.(newState);
    },
    [onStateChange],
  );

  // ── Barge-in: stop audio when user starts speaking ──────────────────────
  const stopCurrentAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    // Notify server that TTS was interrupted
    if (
      nluWs.current &&
      nluWs.current.readyState === WebSocket.OPEN &&
      stateRef.current === "speaking"
    ) {
      nluWs.current.send(JSON.stringify({ type: "tts_done" }));
    }
  }, []);

  // ── Play audio from URL (cached MP3) or fall back to Deepgram TTS ────────
  const playAudio = useCallback(
    async (audioUrl: string | null, replyText: string) => {
      setVoiceState("speaking");

      // Stop any currently playing audio first
      stopCurrentAudio();

      const resolvedUrl = audioUrl
        ? // The Python media server serves cached files from port 8080
          `http://localhost:8080${audioUrl}`
        : null;

      // Try to play cached MP3 first (0ms latency)
      if (resolvedUrl) {
        const audio = new Audio(resolvedUrl);
        audioRef.current = audio;

        await new Promise<void>((resolve) => {
          audio.onended = () => resolve();
          audio.onerror = () => resolve(); // fallback to TTS on error
          audio.play().catch(() => resolve());
        });

        // Notify server that TTS finished
        if (nluWs.current?.readyState === WebSocket.OPEN) {
          nluWs.current.send(JSON.stringify({ type: "tts_done" }));
        }
        setVoiceState("listening");
        return;
      }

      // Fallback: Deepgram TTS for dynamic responses (no cached audio)
      if (!apiKey || !replyText) {
        setVoiceState("listening");
        return;
      }

      try {
        const response = await fetch(
          "https://api.deepgram.com/v1/speak?model=aura-luna-en",
          {
            method: "POST",
            headers: {
              Authorization: `Token ${apiKey}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ text: replyText }),
          },
        );

        if (!response.ok) throw new Error(`Deepgram TTS error: ${response.status}`);

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;

        await new Promise<void>((resolve) => {
          audio.onended = () => {
            URL.revokeObjectURL(url);
            resolve();
          };
          audio.onerror = () => resolve();
          audio.play().catch(() => resolve());
        });
      } catch (e) {
        console.error("[NluVoice] TTS playback failed:", e);
      }

      if (nluWs.current?.readyState === WebSocket.OPEN) {
        nluWs.current.send(JSON.stringify({ type: "tts_done" }));
      }
      setVoiceState("listening");
    },
    [apiKey, setVoiceState, stopCurrentAudio],
  );

  // ── Send a transcript to the NLU server ──────────────────────────────────
  const sendTranscript = useCallback(
    (text: string) => {
      if (!text.trim() || nluWs.current?.readyState !== WebSocket.OPEN) return;
      setLastTranscript(text);
      setVoiceState("thinking");
      nluWs.current.send(JSON.stringify({ type: "transcript", text }));
    },
    [setVoiceState],
  );

  // ── Stream audio chunk to Deepgram STT WebSocket ──────────────────────────
  const sendAudioToDeepgram = useCallback((audioData: Blob) => {
    if (dgWs.current?.readyState === WebSocket.OPEN) {
      dgWs.current.send(audioData);
    }
  }, []);

  // ── Open Deepgram streaming STT WebSocket ─────────────────────────────────
  const openDeepgramStream = useCallback(() => {
    if (!apiKey) {
      console.error("[NluVoice] No Deepgram API key provided.");
      return;
    }

    // Close any existing Deepgram stream
    if (dgWs.current) {
      dgWs.current.close();
    }

    const dg = new WebSocket(
      `wss://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&interim_results=false&endpointing=false`,
      ["token", apiKey],
    );
    dgWs.current = dg;

    dg.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const transcript =
          data?.channel?.alternatives?.[0]?.transcript ?? "";

        if (data.is_final && transcript.trim()) {
          console.log(`[STT] Final: "${transcript}"`);
          sendTranscript(transcript);
        }
      } catch (e) {
        // ignore
      }
    };

    dg.onerror = (e) => console.error("[Deepgram STT] WebSocket error:", e);
  }, [apiKey, sendTranscript]);

  // ── Initialise VAD ───────────────────────────────────────────────────────
  const initVad = useCallback(async () => {
    try {
      // Dynamically import to avoid SSR issues
      const { MicVAD } = await import("@ricky0123/vad-web");

      const vad = await MicVAD.new({
        // Use browser's built-in echo cancellation and noise suppression
        additionalAudioConstraints: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },

        onSpeechStart: () => {
          console.log("[VAD] Speech started");
          // Barge-in: stop robot talking
          stopCurrentAudio();
          // Open a fresh Deepgram stream for this utterance
          openDeepgramStream();
          // Notify the server
          if (nluWs.current?.readyState === WebSocket.OPEN) {
            nluWs.current.send(JSON.stringify({ type: "user_speaking" }));
          }
          setVoiceState("listening");
        },

        onFrameProcessed: (probabilities, audio) => {
          // Stream each audio frame to Deepgram while the user is speaking
          if (
            dgWs.current?.readyState === WebSocket.OPEN &&
            stateRef.current !== "speaking"
          ) {
            const blob = new Blob([audio.buffer], { type: "audio/pcm" });
            sendAudioToDeepgram(blob);
          }
        },

        onSpeechEnd: (audio) => {
          console.log("[VAD] Speech ended — closing Deepgram stream");
          // Send the remaining audio and close the stream to flush the final transcript
          if (dgWs.current?.readyState === WebSocket.OPEN) {
            const blob = new Blob([audio.buffer], { type: "audio/pcm" });
            dgWs.current.send(blob);
            // Send CloseStream message to Deepgram to get the final transcript
            dgWs.current.send(JSON.stringify({ type: "CloseStream" }));
          }
        },

        // Tune VAD sensitivity
        positiveSpeechThreshold: 0.85,
        negativeSpeechThreshold: 0.5,
        minSpeechFrames: 4,
        redemptionFrames: 8,
      });

      vadRef.current = vad;
      return vad;
    } catch (e) {
      console.error("[NluVoice] VAD init failed:", e);
      throw e;
    }
  }, [openDeepgramStream, sendAudioToDeepgram, setVoiceState, stopCurrentAudio]);

  // ── Connect NLU WebSocket ─────────────────────────────────────────────────
  const connectNluServer = useCallback(() => {
    if (nluWs.current?.readyState === WebSocket.OPEN) return;

    console.log(`[NluVoice] Connecting to NLU server: ${nluServerUrl}`);
    const ws = new WebSocket(nluServerUrl);
    nluWs.current = ws;

    ws.onopen = () => {
      console.log("[NluVoice] NLU server connected.");
      setVoiceState("listening");
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === "response") {
        const response: NluResponse = {
          reply_text: msg.reply_text,
          audio_url: msg.audio_url,
          action: msg.action,
        };
        onResponse?.(response);
        playAudio(msg.audio_url, msg.reply_text);
      } else if (msg.type === "state") {
        setVoiceState(msg.conv_state as NluVoiceState);
      }
    };

    ws.onclose = () => {
      console.log("[NluVoice] NLU server disconnected.");
      if (isActive) {
        // Auto-reconnect after 2s if user hasn't manually stopped
        setTimeout(connectNluServer, 2000);
      }
    };

    ws.onerror = (e) => {
      console.error("[NluVoice] NLU WebSocket error:", e);
      setVoiceState("error");
    };
  }, [nluServerUrl, isActive, onResponse, playAudio, setVoiceState]);

  // ── Start voice session ───────────────────────────────────────────────────
  const start = useCallback(async () => {
    if (isActive) return;
    setIsActive(true);
    setVoiceState("connecting");

    try {
      connectNluServer();
      const vad = await initVad();
      vad.start();
      setVoiceState("listening");
    } catch (e) {
      console.error("[NluVoice] Failed to start:", e);
      setVoiceState("error");
      setIsActive(false);
    }
  }, [isActive, connectNluServer, initVad, setVoiceState]);

  // ── Stop voice session ────────────────────────────────────────────────────
  const stop = useCallback(() => {
    setIsActive(false);
    vadRef.current?.destroy();
    vadRef.current = null;
    dgWs.current?.close();
    dgWs.current = null;
    nluWs.current?.close();
    nluWs.current = null;
    stopCurrentAudio();
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    setVoiceState("idle");
  }, [setVoiceState, stopCurrentAudio]);

  // Cleanup on unmount
  useEffect(() => {
    return () => stop();
  }, [stop]);

  return {
    state,
    isActive,
    lastTranscript,
    start,
    stop,
  };
}
