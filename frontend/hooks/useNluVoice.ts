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
  /** Each entry is either a plain string (navigation/smalltalk) or an
   *  event descriptor object { label, filename, category } from the NLU server. */
  suggested_buttons?: Array<string | { label: string; filename: string; category: string }>;
  [key: string]: unknown;
}

export interface NluResponse {
  reply_text: string;
  audio_url: string | null;
  action: NluAction | null;
  utterance_id?: string;
  duration_ms?: number;
}

interface UseNluVoiceOptions {
  nluServerUrl?: string;
  deepgramApiKey?: string;
  onResponse?: (response: NluResponse) => void;
  onStateChange?: (state: NluVoiceState) => void;
  onVolumeChange?: (volume: number) => void;
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
  onVolumeChange,
}: UseNluVoiceOptions = {}) {
  const [state, setState] = useState<NluVoiceState>("idle");
  const [isActive, setIsActive] = useState(false);
  const [lastTranscript, setLastTranscript] = useState("");

  const nluWs = useRef<WebSocket | null>(null);
  const dgWs = useRef<WebSocket | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const volumeAnimRef = useRef<number | null>(null);
  
  const stateRef = useRef<NluVoiceState>("idle");
  const isActiveRef = useRef(false);
  /** Last non-empty transcript for UtteranceEnd fallback. */
  const pendingTranscriptRef = useRef("");
  /** Avoid double-sending the same utterance. */
  const sentUtteranceRef = useRef(false);
  const resumeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dgKeepAliveIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startMicCaptureRef = useRef<() => Promise<void>>(null as any);
  const eventKeywordsRef = useRef<string[]>([]);
  /** Timestamp when speaking ends — used to reject Deepgram echo transcripts */
  const speakingEndedAtRef = useRef<number>(0);
  /** How long (ms) to reject Deepgram results after TTS ends (echo cooldown) */
  const ECHO_COOLDOWN_MS = 2000;

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

  const onVolumeChangeRef = useRef(onVolumeChange);
  onVolumeChangeRef.current = onVolumeChange;

  const stopCurrentAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    if (resumeTimeoutRef.current) {
      clearTimeout(resumeTimeoutRef.current);
      resumeTimeoutRef.current = null;
    }
    if (
      nluWs.current?.readyState === WebSocket.OPEN &&
      stateRef.current === "speaking"
    ) {
      nluWs.current.send(JSON.stringify({ type: "tts_done" }));
    }
  }, []);

  const sendPlaybackStart = useCallback((utteranceId: string) => {
    if (!utteranceId || nluWs.current?.readyState !== WebSocket.OPEN) return;
    nluWs.current.send(
      JSON.stringify({ type: "playback_start", utterance_id: utteranceId }),
    );
  }, []);

  const playAudio = useCallback(
    async (
      audioUrl: string | null,
      replyText: string,
      utteranceId?: string,
      durationMs?: number,
    ) => {
      console.log("[NluVoice] Playing reply:", replyText?.slice(0, 80), audioUrl);
      setVoiceState("speaking");
      stopCurrentAudio();

      const backendHost = typeof window !== "undefined" ? window.location.hostname : "localhost";
      const resolvedUrl = audioUrl
        ? `http://${backendHost}:8080${audioUrl}`
        : null;

      // While the robot talks, stop feeding the mic to Deepgram — otherwise
      // speaker echo triggers SpeechStarted and immediately kills playback.
      const resumeAfter = () => {
        sentUtteranceRef.current = false;
        pendingTranscriptRef.current = "";
        // Mark when speaking ended so we can reject echo transcripts
        speakingEndedAtRef.current = Date.now();
        if (resumeTimeoutRef.current) {
          clearTimeout(resumeTimeoutRef.current);
        }
        resumeTimeoutRef.current = setTimeout(() => {
          setVoiceState("listening");
          resumeTimeoutRef.current = null;
        }, ECHO_COOLDOWN_MS);
      };

      if (resolvedUrl) {
        const audio = new Audio(resolvedUrl);
        audioRef.current = audio;
        try {
          await audio.play();
          sendPlaybackStart(utteranceId ?? "");
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
        if (utteranceId) {
          sendPlaybackStart(utteranceId);
          await new Promise((r) =>
            setTimeout(r, Math.max(800, durationMs ?? 2000)),
          );
        }
        if (nluWs.current?.readyState === WebSocket.OPEN) {
          nluWs.current.send(JSON.stringify({ type: "tts_done" }));
        }
        resumeAfter();
        return;
      }

      try {
        const url = `/api/tts?text=${encodeURIComponent(replyText)}`;
        const audio = new Audio(url);
        audioRef.current = audio;
        await audio.play();
        sendPlaybackStart(utteranceId ?? "");
        await new Promise<void>((resolve) => {
          audio.onended = () => resolve();
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
    [apiKey, setVoiceState, stopCurrentAudio, sendPlaybackStart],
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

      // Reject anything from Deepgram during the echo cooldown window.
      // After TTS ends, the mic may still pick up speaker tail-echo
      // which Deepgram transcribes and sends back — causing an infinite loop.
      const msSinceSpeaking = Date.now() - speakingEndedAtRef.current;
      const inEchoCooldown = msSinceSpeaking < ECHO_COOLDOWN_MS;

      // Deepgram VAD: user started talking → barge-in (only while listening)
      if (data.type === "SpeechStarted") {
        if (busy || inEchoCooldown) {
          // Ignore speaker echo while we play a reply / wait for NLU / cooldown
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

      // Ignore STT while robot is thinking/speaking or during echo cooldown
      if (busy || inEchoCooldown) return;

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

      // Speculative intent: send intermediate transcript if not speech_final
      if (!data.speech_final && !sentUtteranceRef.current && transcript.trim().length > 3) {
        if (nluWs.current?.readyState === WebSocket.OPEN) {
          nluWs.current.send(JSON.stringify({
            type: "speculative_transcript",
            text: transcript.trim()
          }));
        }
      }

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
      endpointing: "300", // Fast response time
      utterance_end_ms: "1000",
    });

    const cleanKw = (text: string) => 
      text.replace(/[^a-zA-Z0-9 ]/g, "").replace(/\s+/g, " ").trim().toLowerCase();

    const baseKeywords = [
      "lab 8",
      "lab 7",
      "deans office", // Removed apostrophe explicitly
      "undergraduate department",
      "lecture hall",
      "front desk",
      "nema",
      "fit24",
      "idealize uom"
    ];

    const rawDomainKeywords = [
      ...baseKeywords.map(k => cleanKw(k)),
      ...eventKeywordsRef.current,
    ];
    
    // Deduplicate! Deepgram might 400 if the same keyterm is passed multiple times
    const uniqueKeywords = Array.from(new Set(rawDomainKeywords));
    uniqueKeywords.forEach((kw) => params.append("keyterm", kw));

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

        // Start KeepAlive heartbeat (every 4s) to prevent Deepgram from dropping idle connections
        if (dgKeepAliveIntervalRef.current) clearInterval(dgKeepAliveIntervalRef.current);
        dgKeepAliveIntervalRef.current = setInterval(() => {
          if (dgWs.current?.readyState === WebSocket.OPEN) {
            dgWs.current.send(JSON.stringify({ type: "KeepAlive" }));
          }
        }, 4000);

        resolve();
      };

      dg.onmessage = (event) => {
        if (typeof event.data === "string") {
          handleDeepgramMessage(event.data);
        }
      };

      dg.onerror = (e) => {
        console.error("[Deepgram STT] WebSocket error:", e);
        if (dgKeepAliveIntervalRef.current) {
          clearInterval(dgKeepAliveIntervalRef.current);
          dgKeepAliveIntervalRef.current = null;
        }
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error("Deepgram WebSocket error"));
        }
      };

      dg.onclose = () => {
        console.log("[NluVoice] Deepgram disconnected.");
        if (dgKeepAliveIntervalRef.current) {
          clearInterval(dgKeepAliveIntervalRef.current);
          dgKeepAliveIntervalRef.current = null;
        }
        if (isActiveRef.current) {
          console.log("[NluVoice] Reconnecting Deepgram stream...");
          setTimeout(async () => {
            try {
              await openDeepgramStream();
              await startMicCaptureRef.current?.();
              console.log("[NluVoice] Deepgram stream successfully reconnected.");
            } catch (e) {
              console.error("[NluVoice] Deepgram reconnect failed:", e);
            }
          }, 2000);
        }
      };
    });
  }, [apiKey, handleDeepgramMessage]);

  const startMicCapture = useCallback(async () => {
    let stream = mediaStreamRef.current;
    const isStreamActive = stream && stream.getAudioTracks().some(track => track.readyState === "live");

    if (!isStreamActive) {
      if (audioCtxRef.current) {
        try {
          audioCtxRef.current.close();
        } catch (e) {}
        audioCtxRef.current = null;
      }
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      mediaStreamRef.current = stream;
    }

    if (recorderRef.current) {
      try {
        recorderRef.current.stop();
      } catch (e) {}
      recorderRef.current = null;
    }

    const mime = pickRecorderMime();
    const recorder = mime
      ? new MediaRecorder(stream!, { mimeType: mime })
      : new MediaRecorder(stream!);
    recorderRef.current = recorder;

    recorder.ondataavailable = (event) => {
      if (
        event.data.size > 0 &&
        dgWs.current?.readyState === WebSocket.OPEN &&
        isActiveRef.current
      ) {
        event.data.arrayBuffer().then((buf) => {
          if (dgWs.current?.readyState === WebSocket.OPEN) {
            // While speaking/thinking, send zeroed audio buffers instead of dropping chunks.
            // This preserves WebM container structure & timestamp alignment so Deepgram
            // STT doesn't corrupt/drop the stream.
            if (stateRef.current === "speaking" || stateRef.current === "thinking") {
              const silentBuf = new ArrayBuffer(buf.byteLength);
              dgWs.current.send(silentBuf);
            } else {
              dgWs.current.send(buf);
            }
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

  startMicCaptureRef.current = startMicCapture;

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

    const backendHost = typeof window !== "undefined" ? window.location.hostname : "localhost";
    const resolvedNluUrl = nluServerUrl.replace("localhost", backendHost).replace("127.0.0.1", backendHost);
    console.log(`[NluVoice] Connecting to NLU server: ${resolvedNluUrl}`);

    return new Promise((resolve, reject) => {
      const ws = new WebSocket(resolvedNluUrl);
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
            utterance_id: msg.utterance_id,
            duration_ms: msg.duration_ms,
          });
          void playAudio(
            msg.audio_url,
            msg.reply_text,
            msg.utterance_id,
            msg.duration_ms,
          );
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

    if (volumeAnimRef.current) {
      cancelAnimationFrame(volumeAnimRef.current);
      volumeAnimRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    analyserRef.current = null;

    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;

    // Ask Deepgram to flush, then close
    if (dgKeepAliveIntervalRef.current) {
      clearInterval(dgKeepAliveIntervalRef.current);
      dgKeepAliveIntervalRef.current = null;
    }
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

  useEffect(() => {
    const fetchKeywords = async () => {
      try {
        const res = await fetch("/api/upload-status");
        const data = await res.json();
        if (data.allFiles) {
          const cleanKw = (text: string) => 
            text.replace(/[^a-zA-Z0-9 ]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
          const kws = data.allFiles
            .map((f: any) => f.extracted?.title)
            .filter(Boolean)
            .map((title: string) => {
              const cleaned = cleanKw(title);
              if (!cleaned || cleaned.length < 3 || cleaned.length > 39) return null;
              return cleaned;
            })
            .filter(Boolean) as string[];
          eventKeywordsRef.current = kws.slice(0, 80);
        }
      } catch (e) {
        console.warn("Failed to fetch dynamic event keywords on mount", e);
      }
    };
    if (apiKey) {
      void fetchKeywords();
    }
  }, [apiKey]);

  return {
    state,
    isActive,
    lastTranscript,
    start,
    stop,
    sendSimulatedVoice: sendTranscript,
  };
}
