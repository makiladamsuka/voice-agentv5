/**
 * useNluVoice.ts
 *
 * Browser voice pipeline for NLU mode (no LiveKit, no Silero ONNX):
 *   1. Mic → MediaRecorder (webm/opus) streamed to Deepgram.
 *   2. Deepgram VAD + endpointing detects speech start/end.
 *   3. Final utterance transcript → local NLU WebSocket.
 *   4. NLU replies with text + cached MP3 (or Deepgram TTS fallback).
 *   5. Barge-in: Deepgram SpeechStarted stops robot audio.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type NluVoiceState =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "connecting"
  | "error";

/** Action payload from the NLU server. Navigate actions carry full map data. */
export interface NluAction {
  action?: string;
  target?: string;
  destination?: string;
  floor?: string;
  path?: number[][];
  path_coords?: number[][];
  path_ids?: string[];
  nodes?: Array<{
    id: string;
    label: string;
    type: string;
    world: number[];
    building?: string | null;
    size?: number[];
    floor?: string;
  }>;
  buildings?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface NluResponse {
  reply_text: string;
  audio_url: string | null;
  action: NluAction | null;
}

interface UseNluVoiceOptions {
  nluServerUrl?: string;
  deepgramApiKey?: string;
  onResponse?: (response: NluResponse) => void;
  onStateChange?: (state: NluVoiceState) => void;
}

/** Prefer a MediaRecorder mime type the browser actually supports. */
function pickRecorderMime(): string | undefined {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];
  for (const t of candidates) {
    if (
      typeof MediaRecorder !== "undefined" &&
      MediaRecorder.isTypeSupported(t)
    ) {
      return t;
    }
  }
  return undefined;
}

export function useNluVoice({
  nluServerUrl = "ws://localhost:8765/ws/voice",
  deepgramApiKey,
  onResponse,
  onStateChange,
}: UseNluVoiceOptions = {}) {
  const [state, setState] = useState<NluVoiceState>("idle");
  const [isActive, setIsActive] = useState(false);
  const [lastTranscript, setLastTranscript] = useState("");

  const nluWs = useRef<WebSocket | null>(null);
  const dgWs = useRef<WebSocket | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stateRef = useRef<NluVoiceState>("idle");
  const isActiveRef = useRef(false);
  /** Last non-empty transcript for UtteranceEnd fallback. */
  const pendingTranscriptRef = useRef("");
  /** Avoid double-sending the same utterance. */
  const sentUtteranceRef = useRef(false);

  const apiKey =
    deepgramApiKey ?? process.env.NEXT_PUBLIC_DEEPGRAM_API_KEY ?? "";

  const setVoiceState = useCallback(
    (newState: NluVoiceState) => {
      stateRef.current = newState;
      setState(newState);
      onStateChange?.(newState);
    },
    [onStateChange],
  );

  const stopCurrentAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    if (
      nluWs.current?.readyState === WebSocket.OPEN &&
      stateRef.current === "speaking"
    ) {
      nluWs.current.send(JSON.stringify({ type: "tts_done" }));
    }
  }, []);

  const playAudio = useCallback(
    async (audioUrl: string | null, replyText: string) => {
      console.log("[NluVoice] Playing reply:", replyText?.slice(0, 80), audioUrl);
      setVoiceState("speaking");
      stopCurrentAudio();

      const resolvedUrl = audioUrl
        ? `http://localhost:8080${audioUrl}`
        : null;

      // While the robot talks, stop feeding the mic to Deepgram — otherwise
      // speaker echo triggers SpeechStarted and immediately kills playback.
      const resumeAfter = () => {
        sentUtteranceRef.current = false;
        pendingTranscriptRef.current = "";
        setVoiceState("listening");
      };

      if (resolvedUrl) {
        const audio = new Audio(resolvedUrl);
        audioRef.current = audio;
        try {
          await audio.play();
          await new Promise<void>((resolve) => {
            audio.onended = () => resolve();
            audio.onerror = () => resolve();
          });
          if (nluWs.current?.readyState === WebSocket.OPEN) {
            nluWs.current.send(JSON.stringify({ type: "tts_done" }));
          }
          resumeAfter();
          return;
        } catch (e) {
          console.warn("[NluVoice] Cached audio play blocked/failed:", e);
          // fall through to Deepgram TTS
        }
      }

      if (!apiKey || !replyText) {
        if (nluWs.current?.readyState === WebSocket.OPEN) {
          nluWs.current.send(JSON.stringify({ type: "tts_done" }));
        }
        resumeAfter();
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
        if (!response.ok) {
          throw new Error(`Deepgram TTS error: ${response.status}`);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;
        await audio.play();
        await new Promise<void>((resolve) => {
          audio.onended = () => {
            URL.revokeObjectURL(url);
            resolve();
          };
          audio.onerror = () => resolve();
        });
      } catch (e) {
        console.error("[NluVoice] TTS playback failed:", e);
      }

      if (nluWs.current?.readyState === WebSocket.OPEN) {
        nluWs.current.send(JSON.stringify({ type: "tts_done" }));
      }
      resumeAfter();
    },
    [apiKey, setVoiceState, stopCurrentAudio],
  );

  const sendTranscript = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || nluWs.current?.readyState !== WebSocket.OPEN) return;
      console.log(`[NluVoice] → NLU: "${trimmed}"`);
      setLastTranscript(trimmed);
      setVoiceState("thinking");
      nluWs.current.send(JSON.stringify({ type: "transcript", text: trimmed }));
    },
    [setVoiceState],
  );

  const handleDeepgramMessage = useCallback(
    (raw: string) => {
      let data: any;
      try {
        data = JSON.parse(raw);
      } catch {
        return;
      }

      const busy =
        stateRef.current === "speaking" || stateRef.current === "thinking";

      // Deepgram VAD: user started talking → barge-in (only while listening)
      if (data.type === "SpeechStarted") {
        if (busy) {
          // Ignore speaker echo while we play a reply / wait for NLU
          return;
        }
        console.log("[Deepgram VAD] SpeechStarted");
        sentUtteranceRef.current = false;
        pendingTranscriptRef.current = "";
        if (nluWs.current?.readyState === WebSocket.OPEN) {
          nluWs.current.send(JSON.stringify({ type: "user_speaking" }));
        }
        setVoiceState("listening");
        return;
      }

      // Ignore STT while robot is thinking/speaking (echo + overlap)
      if (busy) return;

      // End-of-utterance (word-timing based). Flush pending if speech_final missed.
      if (data.type === "UtteranceEnd") {
        console.log("[Deepgram VAD] UtteranceEnd");
        if (!sentUtteranceRef.current && pendingTranscriptRef.current.trim()) {
          sentUtteranceRef.current = true;
          sendTranscript(pendingTranscriptRef.current);
          pendingTranscriptRef.current = "";
        }
        return;
      }

      const alt = data?.channel?.alternatives?.[0];
      const transcript: string = alt?.transcript ?? "";
      if (!transcript.trim()) return;

      // Keep latest interim for UtteranceEnd fallback; show live text via pending
      pendingTranscriptRef.current = transcript;

      // Endpointing: speech_final === end of spoken turn
      if (data.speech_final && !sentUtteranceRef.current) {
        console.log(`[STT] speech_final: "${transcript}"`);
        sentUtteranceRef.current = true;
        pendingTranscriptRef.current = "";
        sendTranscript(transcript);
      }
    },
    [sendTranscript, setVoiceState],
  );

  const openDeepgramStream = useCallback((): Promise<void> => {
    if (!apiKey) {
      return Promise.reject(new Error("Missing Deepgram API key"));
    }
    if (dgWs.current) {
      try {
        dgWs.current.close();
      } catch {
        /* ignore */
      }
      dgWs.current = null;
    }

    // Deepgram's own VAD + endpointing — no Silero / ONNX in the browser.
    const params = new URLSearchParams({
      model: "nova-3",
      smart_format: "true",
      interim_results: "true",
      vad_events: "true",
      endpointing: "300",
      utterance_end_ms: "1000",
    });
    const url = `wss://api.deepgram.com/v1/listen?${params}`;
    console.log("[NluVoice] Opening Deepgram stream (built-in VAD)…");

    return new Promise((resolve, reject) => {
      const dg = new WebSocket(url, ["token", apiKey]);
      dgWs.current = dg;
      let settled = false;

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          reject(new Error("Deepgram WebSocket connect timeout"));
        }
      }, 10000);

      dg.onopen = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        console.log("[NluVoice] Deepgram connected.");
        resolve();
      };

      dg.onmessage = (event) => {
        if (typeof event.data === "string") {
          handleDeepgramMessage(event.data);
        }
      };

      dg.onerror = (e) => {
        console.error("[Deepgram STT] WebSocket error:", e);
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error("Deepgram WebSocket error"));
        }
      };

      dg.onclose = () => {
        console.log("[NluVoice] Deepgram disconnected.");
      };
    });
  }, [apiKey, handleDeepgramMessage]);

  const startMicCapture = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    mediaStreamRef.current = stream;

    const mime = pickRecorderMime();
    const recorder = mime
      ? new MediaRecorder(stream, { mimeType: mime })
      : new MediaRecorder(stream);
    recorderRef.current = recorder;

    recorder.ondataavailable = (event) => {
      if (
        event.data.size > 0 &&
        dgWs.current?.readyState === WebSocket.OPEN &&
        isActiveRef.current &&
        // Do not stream mic→Deepgram while NLU is thinking or TTS is playing
        // (speaker echo would fake SpeechStarted and kill the reply).
        stateRef.current !== "speaking" &&
        stateRef.current !== "thinking"
      ) {
        event.data.arrayBuffer().then((buf) => {
          if (
            dgWs.current?.readyState === WebSocket.OPEN &&
            stateRef.current !== "speaking" &&
            stateRef.current !== "thinking"
          ) {
            dgWs.current.send(buf);
          }
        });
      }
    };

    recorder.onerror = (e) => {
      console.error("[NluVoice] MediaRecorder error:", e);
    };

    // Small timeslice → low-latency chunks for Deepgram VAD
    recorder.start(250);
    console.log("[NluVoice] Mic capture started", mime ?? "(default mime)");
  }, []);

  const connectNluServer = useCallback((): Promise<void> => {
    if (nluWs.current?.readyState === WebSocket.OPEN) {
      return Promise.resolve();
    }
    if (nluWs.current) {
      try {
        nluWs.current.close();
      } catch {
        /* ignore */
      }
      nluWs.current = null;
    }

    console.log(`[NluVoice] Connecting to NLU server: ${nluServerUrl}`);

    return new Promise((resolve, reject) => {
      const ws = new WebSocket(nluServerUrl);
      nluWs.current = ws;
      let settled = false;

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          try {
            ws.close();
          } catch {
            /* ignore */
          }
          reject(new Error(`NLU WebSocket connect timeout: ${nluServerUrl}`));
        }
      }, 8000);

      ws.onopen = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        console.log("[NluVoice] NLU server connected.");
        resolve();
      };

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "response") {
          console.log("[NluVoice] ← NLU response:", msg.reply_text);
          onResponse?.({
            reply_text: msg.reply_text,
            audio_url: msg.audio_url,
            action: msg.action,
          });
          void playAudio(msg.audio_url, msg.reply_text);
        } else if (msg.type === "state") {
          // Don't let server "listening" clobber local speaking/thinking mid-turn
          if (
            msg.conv_state === "listening" &&
            (stateRef.current === "speaking" || stateRef.current === "thinking")
          ) {
            return;
          }
          setVoiceState(msg.conv_state as NluVoiceState);
        }
      };

      ws.onclose = () => {
        console.log("[NluVoice] NLU server disconnected.");
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error("NLU WebSocket closed before open"));
        } else if (isActiveRef.current) {
          setTimeout(() => {
            connectNluServer().catch((e) =>
              console.error("[NluVoice] reconnect failed:", e),
            );
          }, 2000);
        }
      };

      ws.onerror = (e) => {
        console.error("[NluVoice] NLU WebSocket error:", e);
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(
            new Error(`NLU WebSocket error — is the backend on ${nluServerUrl}?`),
          );
        } else {
          setVoiceState("error");
        }
      };
    });
  }, [nluServerUrl, onResponse, playAudio, setVoiceState]);

  const start = useCallback(async () => {
    if (isActiveRef.current) {
      console.warn("[NluVoice] start() ignored — already active");
      return;
    }
    if (!apiKey) {
      const err = new Error(
        "Missing NEXT_PUBLIC_DEEPGRAM_API_KEY — cannot start NLU voice.",
      );
      console.error("[NluVoice]", err.message);
      setVoiceState("error");
      throw err;
    }

    isActiveRef.current = true;
    setIsActive(true);
    setVoiceState("connecting");
    pendingTranscriptRef.current = "";
    sentUtteranceRef.current = false;

    try {
      await connectNluServer();
      await openDeepgramStream();
      await startMicCapture();
      setVoiceState("listening");
      console.log("[NluVoice] Listening (Deepgram VAD) — speak now");
    } catch (e) {
      console.error("[NluVoice] Failed to start:", e);
      isActiveRef.current = false;
      setIsActive(false);
      setVoiceState("error");
      try {
        recorderRef.current?.stop();
      } catch {
        /* ignore */
      }
      recorderRef.current = null;
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
      mediaStreamRef.current = null;
      try {
        dgWs.current?.close();
      } catch {
        /* ignore */
      }
      dgWs.current = null;
      try {
        nluWs.current?.close();
      } catch {
        /* ignore */
      }
      nluWs.current = null;
      throw e;
    }
  }, [
    apiKey,
    connectNluServer,
    openDeepgramStream,
    setVoiceState,
    startMicCapture,
  ]);

  const stop = useCallback(() => {
    isActiveRef.current = false;
    setIsActive(false);

    try {
      if (recorderRef.current?.state !== "inactive") {
        recorderRef.current?.stop();
      }
    } catch {
      /* ignore */
    }
    recorderRef.current = null;

    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;

    // Ask Deepgram to flush, then close
    try {
      if (dgWs.current?.readyState === WebSocket.OPEN) {
        dgWs.current.send(JSON.stringify({ type: "CloseStream" }));
      }
      dgWs.current?.close();
    } catch {
      /* ignore */
    }
    dgWs.current = null;

    try {
      nluWs.current?.close();
    } catch {
      /* ignore */
    }
    nluWs.current = null;

    stopCurrentAudio();
    setVoiceState("idle");
  }, [setVoiceState, stopCurrentAudio]);

  const stopRef = useRef(stop);
  stopRef.current = stop;

  useEffect(() => {
    return () => {
      stopRef.current();
    };
  }, []);

  useEffect(() => {
    const onInject = (event: Event) => {
      const text = (event as CustomEvent<{ text?: string }>).detail?.text;
      if (text) sendTranscript(text);
    };
    window.addEventListener("nlu:inject_transcript", onInject);
    return () => window.removeEventListener("nlu:inject_transcript", onInject);
  }, [sendTranscript]);

  return {
    state,
    isActive,
    lastTranscript,
    start,
    stop,
  };
}
