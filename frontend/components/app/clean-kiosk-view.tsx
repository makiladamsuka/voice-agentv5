"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ThemeToggle } from "@/components/app/theme-toggle";
import {
  Settings,
  X,
  Wifi,
  RefreshCw,
  Maximize2,
  Minimize2,
  Volume2,
  VolumeX,
  RotateCcw,
} from "lucide-react";
import { GeminiMorphButton } from "@/components/ui/GeminiMorphButton";
import { PopButton } from "@/components/ui/PopButton";
import { useNluAdapter } from "@/hooks/useNluAdapter";
import { EmojiStage } from "@/components/app/emoji-stage";
import { motion, AnimatePresence } from "motion/react";

function Clock() {
  const [time, setTime] = useState("");
  useEffect(() => {
    function update() {
      const now = new Date();
      setTime(
        now.toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      );
    }
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, []);
  return <span>{time}</span>;
}

interface RobotSettingsState {
  faceGreeting: boolean;
  talkGestures: boolean;
  baseHomeSpin: boolean;
  faceTracking: boolean;
}

export function CleanKioskView() {
  const nluAdapter = useNluAdapter();
  const startRef = useRef(nluAdapter.start);
  startRef.current = nluAdapter.start;
  const endRef = useRef(nluAdapter.end);
  endRef.current = nluAdapter.end;

  const startSession = useCallback(async () => {
    await startRef.current();
  }, []);

  const end = useCallback(() => {
    endRef.current();
  }, []);

  // Settings & Kiosk State
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [deviceIp, setDeviceIp] = useState<string>("Checking...");
  const [isReloadingNlu, setIsReloadingNlu] = useState(false);
  const [reloadStatus, setReloadStatus] = useState<string>("");
  const [clearedAt, setClearedAt] = useState<number>(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [volume, setVolume] = useState<number>(0.85);

  // Hardware switches loaded from /api/robot-settings
  const [robotSettings, setRobotSettings] = useState<RobotSettingsState>({
    faceGreeting: true,
    talkGestures: true,
    baseHomeSpin: true,
    faceTracking: true,
  });

  useEffect(() => {
    // 1. Load volume from localStorage and backend /api/volume
    const savedVol = localStorage.getItem("kiosk_volume");
    if (savedVol !== null) {
      const parsed = parseFloat(savedVol);
      if (!isNaN(parsed)) {
        setVolume(parsed);
        (window as any)._masterVolume = parsed;
      }
    } else {
      (window as any)._masterVolume = 0.85;
    }

    fetch("/api/volume")
      .then((res) => res.json())
      .then((data) => {
        if (typeof data.volume === "number") {
          setVolume(data.volume);
          (window as any)._masterVolume = data.volume;
          localStorage.setItem("kiosk_volume", String(data.volume));
        }
      })
      .catch(() => {});

    // 2. Load robot settings
    fetch("/api/robot-settings")
      .then((res) => res.json())
      .then((data) => {
        setRobotSettings({
          faceGreeting: data.faceGreeting ?? true,
          talkGestures: data.talkGestures ?? true,
          baseHomeSpin: data.baseHomeSpin ?? true,
          faceTracking: data.faceTracking ?? true,
        });
      })
      .catch((err) => console.error("Error loading robot settings:", err));

    // 3. Load device IP
    fetch("/api/network-ip")
      .then((res) => res.json())
      .then((data) => setDeviceIp(data.ip || "127.0.0.1"))
      .catch(() => setDeviceIp("Offline"));

    // 4. Fullscreen change listener
    const handleFsChange = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
    };
    document.addEventListener("fullscreenchange", handleFsChange);
    return () => document.removeEventListener("fullscreenchange", handleFsChange);
  }, []);

  const handleVolumeChange = (newVol: number) => {
    const clamped = Math.max(0, Math.min(1, newVol));
    setVolume(clamped);
    (window as any)._masterVolume = clamped;
    localStorage.setItem("kiosk_volume", String(clamped));
    fetch("/api/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: clamped }),
    }).catch(() => {});
  };

  const updateSetting = async (key: keyof RobotSettingsState, value: boolean) => {
    const updated = { ...robotSettings, [key]: value };
    setRobotSettings(updated);
    try {
      await fetch("/api/robot-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value }),
      });
    } catch (e) {
      console.error(`Failed to update ${key}:`, e);
      setRobotSettings((prev) => ({ ...prev, [key]: !value }));
    }
  };

  const toggleFullscreen = async () => {
    try {
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch (err) {
      console.error("Failed to toggle fullscreen:", err);
    }
  };

  const clearTranscript = () => {
    setClearedAt(Date.now());
  };

  const reloadNlu = async () => {
    setIsReloadingNlu(true);
    setReloadStatus("Reloading...");
    try {
      const backendHost = typeof window !== "undefined" ? window.location.hostname : "localhost";
      const res = await fetch(`http://${backendHost}:8765/reload-nlu`, { method: "POST" });
      if (res.ok) {
        setReloadStatus("Refreshed");
      } else {
        setReloadStatus("Failed");
      }
    } catch (e) {
      setReloadStatus("Offline");
    } finally {
      setIsReloadingNlu(false);
      setTimeout(() => setReloadStatus(""), 2500);
    }
  };

  const applyEyeColor = useCallback(
    async (colorName: string, uiTheme: string) => {
      if (uiTheme) {
        document.documentElement.setAttribute("data-pixel-theme", uiTheme);
      } else {
        document.documentElement.removeAttribute("data-pixel-theme");
      }
      try {
        await fetch("/api/eye-color", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ theme: colorName }),
        });
      } catch (e) {
        console.error("Failed to set eye color:", e);
      }
    },
    []
  );

  const isConnected = nluAdapter.isConnected;
  const agentState = nluAdapter.agentState;
  const maxVolume = nluAdapter.maxVolume;
  const sendSimulatedVoice = nluAdapter.sendSimulatedVoice;
  const lastAction = nluAdapter.lastAction;

  const isThinking = agentState === "thinking";
  const isAgentInitializing = agentState === "connecting";

  const handleMicClick = async () => {
    if (!isConnected) {
      await startSession();
    } else {
      end();
    }
  };

  // Derive active caption from latest message or transcription (filtered by clearedAt)
  const activeCaption = useMemo(() => {
    const allMsgs = (nluAdapter.messages || []).filter((m) => (m.timestamp || 0) > clearedAt);
    if (allMsgs.length > 0) {
      const last = allMsgs[allMsgs.length - 1];
      if (last.content) {
        return {
          text: last.content,
          isUser: last.role === "user",
        };
      }
    }
    const allTrans = nluAdapter.transcriptions || [];
    if (allTrans.length > 0 && clearedAt === 0) {
      const last = allTrans[allTrans.length - 1];
      if (last.text) {
        return {
          text: last.text,
          isUser: last.participantIdentity === "user",
        };
      }
    }
    return {
      text: "",
      isUser: false,
    };
  }, [nluAdapter.messages, nluAdapter.transcriptions, clearedAt]);

  return (
    <div className="relative w-screen h-screen bg-white dark:bg-neutral-950 text-neutral-900 dark:text-neutral-50 overflow-hidden flex flex-col justify-between p-6 md:p-8 select-none">
      {/* ── Top Navigation Bar ── */}
      <header className="flex-shrink-0 w-full flex justify-between items-center h-14 z-20">
        <div className="flex items-center gap-3">
          <span className="text-3xl font-black tracking-tight">NEma</span>
        </div>

        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold tracking-wide text-neutral-500 dark:text-neutral-400 font-mono">
            <Clock />
          </span>
          <ThemeToggle />
          <PopButton
            onClick={() => setIsSettingsOpen(!isSettingsOpen)}
            className="p-2.5 rounded-full border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-900 text-neutral-700 dark:text-neutral-200 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors"
            aria-label="Settings"
          >
            <Settings className="w-5 h-5" />
          </PopButton>
        </div>
      </header>

      {/* ── Main Stage: Big Animated Emoji + Big Subtitles ── */}
      <main className="flex-1 flex flex-col items-center justify-center max-w-4xl w-full mx-auto text-center px-4 -mt-2">
        {/* Animated Emoji Stage */}
        <EmojiStage
          transcript={activeCaption.text}
          convState={agentState}
          emotion="idle"
          faceCount={1}
        />

        {/* Hero Transcript Display */}
        <div className="min-h-[140px] flex flex-col items-center justify-center mt-2">
          {activeCaption.text ? (
            <div className="space-y-3">
              {activeCaption.isUser && (
                <span className="inline-block px-3.5 py-1 rounded-full text-xs font-bold uppercase tracking-wider bg-blue-100 dark:bg-blue-950/80 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800">
                  You asked
                </span>
              )}
              <h1 className="text-4xl md:text-6xl font-bold tracking-tight leading-snug">
                {activeCaption.text}
              </h1>
            </div>
          ) : (
            <h1 className="text-4xl md:text-6xl font-bold tracking-tight text-neutral-900 dark:text-neutral-100">
              Welcome to FIT EXPO
            </h1>
          )}
        </div>

        {/* Quick Suggestion Buttons (if any) */}
        {lastAction?.suggested_buttons && lastAction.suggested_buttons.length > 0 && (
          <div className="flex flex-wrap items-center justify-center gap-3 mt-6">
            {lastAction.suggested_buttons.map((btn: any, idx: number) => {
              const label = typeof btn === "string" ? btn : btn.label || "";
              if (!label) return null;
              return (
                <PopButton
                  key={idx}
                  onClick={() => sendSimulatedVoice(label)}
                  className="px-6 py-3 rounded-full bg-neutral-100 dark:bg-neutral-800/80 border border-neutral-200 dark:border-neutral-700 text-base font-semibold hover:bg-neutral-200 dark:hover:bg-neutral-700 transition-colors shadow-sm"
                >
                  {label}
                </PopButton>
              );
            })}
          </div>
        )}
      </main>

      {/* ── Bottom Controls: Minimalist Mic & Single Status Line ── */}
      <footer className="flex-shrink-0 w-full flex flex-col items-center justify-center pb-6 z-20">
        <div className="relative flex items-center justify-center w-28 h-28">
          <GeminiMorphButton
            size={96}
            volume={agentState === "listening" ? maxVolume : 0}
            isAnimating={
              isAgentInitializing ||
              isConnected ||
              isThinking
            }
            isConnected={isConnected}
            onClick={handleMicClick}
          />
        </div>

        {/* Clean, Single Status Label */}
        <p className="text-xl md:text-2xl font-bold text-neutral-800 dark:text-neutral-100 mt-4 tracking-tight">
          {isConnected
            ? isThinking
              ? "Thinking..."
              : agentState === "speaking"
              ? "Speaking..."
              : "Listening... Tap to stop"
            : "Tap to talk"}
        </p>
      </footer>

      {/* ── Settings Modal (Entire modal in one view, completely non-scrollable) ── */}
      <AnimatePresence>
        {isSettingsOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4"
            onClick={() => setIsSettingsOpen(false)}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0, y: 12 }}
              animate={{ scale: 1, opacity: 1, y: 0 }}
              exit={{ scale: 0.95, opacity: 0, y: 12 }}
              transition={{ type: "spring", stiffness: 360, damping: 28 }}
              className="bg-white dark:bg-neutral-900 text-neutral-900 dark:text-neutral-50 w-full max-w-[420px] rounded-3xl border border-neutral-200 dark:border-neutral-800 shadow-2xl p-5 space-y-3 select-none overflow-hidden"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Header */}
              <div className="flex items-center justify-between pb-1 border-b border-neutral-100 dark:border-neutral-800">
                <h3 className="text-xl font-black tracking-tight">Settings</h3>
                <PopButton
                  onClick={() => setIsSettingsOpen(false)}
                  className="p-1.5 rounded-full bg-neutral-100 dark:bg-neutral-800 hover:bg-neutral-200 dark:hover:bg-neutral-700 transition-colors"
                  aria-label="Close Settings"
                >
                  <X className="w-4 h-4" />
                </PopButton>
              </div>

              {/* Speaker Volume Slider Card */}
              <div className="px-3.5 py-2.5 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-800/40 space-y-1.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {volume === 0 ? (
                      <VolumeX className="w-4 h-4 text-neutral-400" />
                    ) : (
                      <Volume2 className="w-4 h-4 text-blue-500" />
                    )}
                    <span className="font-semibold text-sm">Speaker Volume</span>
                  </div>
                  <span className="text-xs font-mono font-bold px-2 py-0.5 rounded bg-white dark:bg-neutral-700 border border-neutral-200 dark:border-neutral-600 text-neutral-800 dark:text-neutral-200 shadow-2xs">
                    {Math.round(volume * 100)}%
                  </span>
                </div>
                <input
                  type="range"
                  min="0"
                  max="100"
                  value={Math.round(volume * 100)}
                  onChange={(e) => handleVolumeChange(Number(e.target.value) / 100)}
                  className="w-full h-2 bg-neutral-200 dark:bg-neutral-700 rounded-lg appearance-none cursor-pointer accent-blue-600 focus:outline-none"
                />
              </div>

              {/* Clean Toggle Rows */}
              <div className="space-y-0.5">
                {/* Auto-Greet */}
                <div className="flex items-center justify-between py-2 border-b border-neutral-100 dark:border-neutral-800/70">
                  <div className="flex flex-col max-w-[78%]">
                    <span className="font-semibold text-sm">Auto-Greet</span>
                    <span className="text-[11px] text-neutral-500 dark:text-neutral-400 leading-tight">
                      Wave and speak when someone walks up
                    </span>
                  </div>
                  <PopButton
                    onClick={() => updateSetting("faceGreeting", !robotSettings.faceGreeting)}
                    className={`w-11 h-6 rounded-full transition-colors relative shrink-0 ${
                      robotSettings.faceGreeting ? "bg-emerald-500" : "bg-neutral-300 dark:bg-neutral-700"
                    }`}
                    aria-label="Toggle Auto Greet"
                  >
                    <span
                      className={`absolute top-1 bottom-1 w-4 bg-white rounded-full transition-all shadow-sm ${
                        robotSettings.faceGreeting ? "left-6" : "left-1"
                      }`}
                    />
                  </PopButton>
                </div>

                {/* Base Home Spin Toggle */}
                <div className="flex items-center justify-between py-2 border-b border-neutral-100 dark:border-neutral-800/70">
                  <div className="flex flex-col max-w-[78%]">
                    <span className="font-semibold text-sm">Base Home Spin</span>
                    <span className="text-[11px] text-neutral-500 dark:text-neutral-400 leading-tight">
                      Rotate base plate back to center when returning home or exiting
                    </span>
                  </div>
                  <PopButton
                    onClick={() => updateSetting("baseHomeSpin", !robotSettings.baseHomeSpin)}
                    className={`w-11 h-6 rounded-full transition-colors relative shrink-0 ${
                      robotSettings.baseHomeSpin ? "bg-emerald-500" : "bg-neutral-300 dark:bg-neutral-700"
                    }`}
                    aria-label="Toggle Base Home Spin"
                  >
                    <span
                      className={`absolute top-1 bottom-1 w-4 bg-white rounded-full transition-all shadow-sm ${
                        robotSettings.baseHomeSpin ? "left-6" : "left-1"
                      }`}
                    />
                  </PopButton>
                </div>

                {/* Arm Gestures */}
                <div className="flex items-center justify-between py-2 border-b border-neutral-100 dark:border-neutral-800/70">
                  <div className="flex flex-col max-w-[78%]">
                    <span className="font-semibold text-sm">Talking Gestures</span>
                    <span className="text-[11px] text-neutral-500 dark:text-neutral-400 leading-tight">
                      Move arms expressively while robot is speaking
                    </span>
                  </div>
                  <PopButton
                    onClick={() => updateSetting("talkGestures", !robotSettings.talkGestures)}
                    className={`w-11 h-6 rounded-full transition-colors relative shrink-0 ${
                      robotSettings.talkGestures ? "bg-emerald-500" : "bg-neutral-300 dark:bg-neutral-700"
                    }`}
                    aria-label="Toggle Talking Gestures"
                  >
                    <span
                      className={`absolute top-1 bottom-1 w-4 bg-white rounded-full transition-all shadow-sm ${
                        robotSettings.talkGestures ? "left-6" : "left-1"
                      }`}
                    />
                  </PopButton>
                </div>

                {/* Face Tracking */}
                <div className="flex items-center justify-between py-2 border-b border-neutral-100 dark:border-neutral-800/70">
                  <div className="flex flex-col max-w-[78%]">
                    <span className="font-semibold text-sm">Face Following</span>
                    <span className="text-[11px] text-neutral-500 dark:text-neutral-400 leading-tight">
                      Pan and tilt head toward detected visitor faces
                    </span>
                  </div>
                  <PopButton
                    onClick={() => updateSetting("faceTracking", !robotSettings.faceTracking)}
                    className={`w-11 h-6 rounded-full transition-colors relative shrink-0 ${
                      robotSettings.faceTracking ? "bg-emerald-500" : "bg-neutral-300 dark:bg-neutral-700"
                    }`}
                    aria-label="Toggle Face Tracking"
                  >
                    <span
                      className={`absolute top-1 bottom-1 w-4 bg-white rounded-full transition-all shadow-sm ${
                        robotSettings.faceTracking ? "left-6" : "left-1"
                      }`}
                    />
                  </PopButton>
                </div>
              </div>

              {/* Eye Color Swatches (All 9 in one row, completely fitting without scroll) */}
              <div className="py-1">
                <div className="flex items-center justify-between w-full">
                  {[
                    { name: "White", eye: "white", ui: "", swatch: "bg-white" },
                    {
                      name: "Pistachio",
                      eye: "pistachio",
                      ui: "pistachio",
                      swatch: "bg-[#93c572]",
                    },
                    {
                      name: "Coral",
                      eye: "coral",
                      ui: "coral",
                      swatch: "bg-[#ff7f50]",
                    },
                    { name: "Blue", eye: "blue", ui: "", swatch: "bg-[#2563EB]" },
                    { name: "Green", eye: "green", ui: "", swatch: "bg-[#10B981]" },
                    { name: "Purple", eye: "purple", ui: "", swatch: "bg-[#7C3AED]" },
                    { name: "Orange", eye: "orange", ui: "", swatch: "bg-[#EA580C]" },
                    { name: "Yellow", eye: "yellow", ui: "", swatch: "bg-yellow-400" },
                    { name: "Red", eye: "red", ui: "", swatch: "bg-[#EF4444]" },
                  ].map((c) => (
                    <PopButton
                      key={c.name}
                      onClick={() => {
                        void applyEyeColor(c.eye, c.ui);
                      }}
                      className={`w-8 h-8 rounded-full shrink-0 shadow-sm transition-transform active:scale-95 ${c.swatch} ${
                        c.name === "White"
                          ? "border-2 border-neutral-300 dark:border-neutral-600 ring-1 ring-neutral-400 dark:ring-neutral-500"
                          : "border border-neutral-300 dark:border-neutral-700"
                      }`}
                      aria-label={`Change eye color to ${c.name}`}
                    />
                  ))}
                </div>
              </div>

              {/* Kiosk Action Buttons */}
              <div className="pt-1.5 border-t border-neutral-100 dark:border-neutral-800/70">
                <div className="grid grid-cols-3 gap-2">
                  {/* Fullscreen Button */}
                  <PopButton
                    onClick={toggleFullscreen}
                    className="py-2.5 px-2 rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-800/50 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors flex items-center justify-center gap-1.5 text-xs font-semibold text-neutral-800 dark:text-neutral-200"
                  >
                    {isFullscreen ? (
                      <Minimize2 className="w-3.5 h-3.5 text-blue-500 shrink-0" />
                    ) : (
                      <Maximize2 className="w-3.5 h-3.5 text-blue-500 shrink-0" />
                    )}
                    <span>{isFullscreen ? "Exit Full" : "Fullscreen"}</span>
                  </PopButton>

                  {/* Clear Screen / Reset Transcript */}
                  <PopButton
                    onClick={clearTranscript}
                    className="py-2.5 px-2 rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-800/50 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors flex items-center justify-center gap-1.5 text-xs font-semibold text-neutral-800 dark:text-neutral-200"
                  >
                    <RotateCcw className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                    <span>Reset</span>
                  </PopButton>

                  {/* Knowledge Sync */}
                  <PopButton
                    onClick={reloadNlu}
                    disabled={isReloadingNlu}
                    className="py-2.5 px-2 rounded-xl border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-800/50 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors flex items-center justify-center gap-1.5 text-xs font-semibold text-neutral-800 dark:text-neutral-200"
                  >
                    <RefreshCw className={`w-3.5 h-3.5 text-emerald-500 shrink-0 ${isReloadingNlu ? "animate-spin" : ""}`} />
                    <span>{reloadStatus || "Sync NLU"}</span>
                  </PopButton>
                </div>
              </div>

              {/* Status Footer */}
              <div className="pt-2 border-t border-neutral-100 dark:border-neutral-800/70 flex items-center justify-between text-xs text-neutral-500 dark:text-neutral-400">
                <div className="flex items-center gap-1.5">
                  {isConnected ? (
                    <>
                      <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                      <span className="font-medium text-emerald-600 dark:text-emerald-400">NLU Online</span>
                    </>
                  ) : (
                    <>
                      <span className="w-2 h-2 rounded-full bg-neutral-400" />
                      <span>NLU Standby</span>
                    </>
                  )}
                </div>

                <div className="flex items-center gap-1.5">
                  <Wifi className="w-3.5 h-3.5" />
                  <span className="font-mono font-semibold text-neutral-700 dark:text-neutral-300">
                    {deviceIp}
                  </span>
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
