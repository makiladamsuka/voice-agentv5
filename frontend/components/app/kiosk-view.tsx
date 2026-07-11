"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable @next/next/no-img-element */

import {
  useSessionContext,
  useSessionMessages,
  useTranscriptions,
  useTracks,
  useTrackVolume,
  useVoiceAssistant,
  useRoomContext,
} from "@livekit/components-react";
import { Track } from "livekit-client";
import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  Suspense,
} from "react";
import { motion, AnimatePresence } from "motion/react";
import { ChatTranscript } from "@/components/app/chat-transcript";
import { ScrollArea } from "@/components/livekit/scroll-area/scroll-area";
import { ThemeToggle } from "@/components/app/theme-toggle";
import { QRCodeSVG } from "qrcode.react";
import { UploadCloud, X, Settings } from "lucide-react";
import dynamic from "next/dynamic";
import LoadingOverlay from "@/components/ui/LoadingOverlay";
import { GeminiMorphButton } from "@/components/ui/GeminiMorphButton";
import { SiriGlow } from "@/components/ui/SiriGlow";
import { ImageDisplay } from "@/components/app/image-display";
import {
  sessionStartOptions,
  useVoiceConfig,
} from "@/hooks/use-voice-config";

// Lazy load 3D map to avoid SSR issues with Three.js
const CampusMapEmbed = dynamic(
  () => import("@/components/app/campus-map-embed"),
  { ssr: false },
);
const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

export function KioskView() {
  const [glowingSection, setGlowingSection] = useState<'where-to' | 'chat' | 'mic' | 'news' | null>(null);
  const session = useSessionContext();
  const { isConnected, start, end } = session;
  const voiceConfig = useVoiceConfig();
  const startSession = useCallback(
    () => start(sessionStartOptions(voiceConfig?.localMic)),
    [start, voiceConfig?.localMic],
  );
  const { messages } = useSessionMessages(session);
  const room = useRoomContext();

  // Focused event state — set when a news card is tapped
  const [focusedEvent, setFocusedEvent] = useState<any | null>(null);
  const pendingEventRef = useRef<any | null>(null);
  const transcriptions = useTranscriptions();
  const [navData, setNavData] = useState<any | null>(null);

  const { audioTrack: agentTrack, state: agentState } = useVoiceAssistant();
  const agentVolume = useTrackVolume(agentTrack);
  const micTracks = useTracks([Track.Source.Microphone]);
  const localMicTrack = micTracks.find((t) => t.participant.isLocal);
  const micVolume = useTrackVolume(localMicTrack);
  const maxVolume = isConnected
    ? Math.max(agentVolume || 0, micVolume || 0)
    : 0;

  const isThinking = agentState === "thinking";
  const isAgentInitializing =
    isConnected &&
    messages.filter((m) => !m.from?.isLocal).length === 0 &&
    transcriptions.length === 0;
  // Dramatically amplify the scaling and opacity for the visual pulse effect
  const pulseScale = isThinking
    ? 1.05
    : !isConnected
      ? undefined
      : 1 + maxVolume * 2.0;
  const pulseOpacity = !isConnected
    ? undefined
    : isThinking
      ? 0.5
      : 0.2 + maxVolume * 0.8;

  const applyEyeColor = useCallback(
    async (eyeTheme: string, uiTheme: string) => {
      if (uiTheme) {
        document.documentElement.setAttribute("data-pixel-theme", uiTheme);
      } else {
        document.documentElement.removeAttribute("data-pixel-theme");
      }

      try {
        await fetch("/api/eye-color", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ theme: eyeTheme }),
        });
      } catch (e) {
        console.error("Failed to set eye color:", e);
      }

      if (room) {
        try {
          room.localParticipant.publishData(
            new TextEncoder().encode(
              JSON.stringify({ type: "theme_change", theme: eyeTheme }),
            ),
            { reliable: true },
          );
        } catch (e) {
          console.error("Failed to publish color data:", e);
        }
      }
    },
    [room],
  );

  // Send event context to backend via LiveKit data channel
  const sendEventFocus = useCallback(
    (event: any) => {
      if (!room) return;
      try {
        const payload = JSON.stringify({ type: "event_focus", event });
        room.localParticipant.publishData(new TextEncoder().encode(payload), {
          reliable: true,
        });
        console.log("📲 Sent event_focus to agent:", event.message);
      } catch (e) {
        console.error("Failed to publish event data:", e);
      }
    },
    [room],
  );

  // When connection established AND there's a pending event, send it
  useEffect(() => {
    if (isConnected && pendingEventRef.current) {
      const ev = pendingEventRef.current;
      pendingEventRef.current = null;
      // Small delay so agent finishes its "I'm ready" greeting first
      setTimeout(() => sendEventFocus(ev), 2500);
    }
  }, [isConnected, sendEventFocus]);

  // Listen for navigation data
  useEffect(() => {
    if (!room) return;
    const handleDataReceived = (payload: Uint8Array) => {
      try {
        const data = JSON.parse(new TextDecoder().decode(payload));
        if (data.type === "navigation") {
          setNavData(data);
        }
      } catch (e) {}
    };
    room.on("dataReceived", handleDataReceived);
    return () => {
      room.off("dataReceived", handleDataReceived);
    };
  }, [room]);

  const [isConnecting, setIsConnecting] = useState(false);

  // Handle clicking a news card
  const handleNewsClick = useCallback(
    async (post: any) => {
      setFocusedEvent(post);
      if (!isConnected) {
        pendingEventRef.current = post;
        await startSession();
      } else {
        sendEventFocus(post);
      }
    },
    [isConnected, startSession, sendEventFocus],
  );

  // Handle mic button press — show blob overlay while connecting
  const handleMicClick = useCallback(async () => {
    if (isConnected) {
      end();
      return;
    }
    setIsConnecting(true);
    try {
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Connection timeout")), 30000)
      );
      await Promise.race([startSession(), timeoutPromise]);
    } catch (e) {
      console.error("Agent connection failed:", e);
      setIsConnecting(false);
    }
  }, [isConnected, startSession, end]);

  // Auto-dismiss morphing button once agent is fully ready
  useEffect(() => {
    if (isConnected && !isAgentInitializing && isConnecting) {
      setIsConnecting(false);
    }
  }, [isConnected, isAgentInitializing, isConnecting]);

  const latestTranscription = transcriptions[transcriptions.length - 1];
  const [stagingText, setStagingText] = useState("");

  useEffect(() => {
    if (latestTranscription && latestTranscription.text) {
      setStagingText(latestTranscription.text);
      const timer = setTimeout(() => {
        setStagingText("");
      }, 2000); // Clear after 2 seconds of silence
      return () => clearTimeout(timer);
    }
  }, [latestTranscription?.text]);

  // Keep other state variables below
  const [time, setTime] = useState("");
  const [dateStr, setDateStr] = useState("");

  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [isColorModalOpen, setIsColorModalOpen] = useState(false);
  const [qrUrl, setQrUrl] = useState("");

  // 3D Map data from saved floor
  const [mapData, setMapData] = useState<any>(null);
  const [mapRooms, setMapRooms] = useState<any[]>([]);

  useEffect(() => {
    fetch("/api/map?floor=floor_1")
      .then((res) => res.json())
      .then((data) => {
        if (data && data.nodes) {
          setMapData(data);
          setMapRooms(data.nodes.filter((n: any) => n.type !== "waypoint"));
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    async function fetchIp() {
      try {
        const res = await fetch("/api/network-ip");
        const data = await res.json();
        if (data.ip) {
          setQrUrl(`http://${data.ip}:3000/upload-portal`);
        } else {
          setQrUrl(`http://${window.location.hostname}:3000/upload-portal`);
        }
      } catch (err) {
        if (typeof window !== "undefined") {
          setQrUrl(`http://${window.location.hostname}:3000/upload-portal`);
        }
      }
    }
    fetchIp();
  }, []);

  const lastKnownUploadRef = useRef(0);

  // Poll for successful uploads to auto-close the modal and show the new poster
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch("/api/upload-status");
        const data = await res.json();

        if (lastKnownUploadRef.current === 0) {
          // Initial load, just sync the current state
          lastKnownUploadRef.current = data.lastUpload;
        } else if (data.lastUpload > lastKnownUploadRef.current) {
          // A new upload happened globally!
          lastKnownUploadRef.current = data.lastUpload;

          // We can still trigger UI changes like resetting current slide or closing modal
          setIsUploadModalOpen(false);
          setCurrentSlide(0);
        }

        // Always sync the local files list dynamically to reflect additions and deletions
        if (data.allFiles) {
          const newLocalPosts = data.allFiles.map((file: any) => {
            const categoryMap: Record<string, string> = {
              events: "Featured Campus Event",
              competitions: "Upcoming Competition",
              posts: "Campus Announcement",
            };
            const defaultTitle =
              categoryMap[file.category] || "Campus Highlight";

            // Prioritize the AI-extracted title
            const title = file.extracted?.title || defaultTitle;

            return {
              id: "local_" + file.mtimeMs + "_" + file.name,
              full_picture: file.url,
              message: title,
              description: file.extracted?.description || "",
              extracted_date: file.extracted?.date || "",
              extracted_time: file.extracted?.time || "",
              extracted_location: file.extracted?.location || "",
              created_time: new Date(file.mtimeMs).toISOString(),
              isLocal: true,
              category: file.category,
            };
          });
          setLocalPosts(newLocalPosts);
        }
      } catch (e) {
        // Ignore errors
      }
    }, 8000);

    return () => clearInterval(interval);
  }, []);


  // Merged Posts State
  const [facebookPosts, setFacebookPosts] = useState<any[]>([]);
  const [localPosts, setLocalPosts] = useState<any[]>([]);
  const fbPosts = [...localPosts, ...facebookPosts];
  const [currentSlide, setCurrentSlide] = useState(0);

  // Standby Rotating Prompts
  const STANDBY_PROMPTS = [
    "Welcome to the Faculty of IT!",
    "Ask for directions to Lab 03",
    "Find the Dean's Office here",
    "Ask about events & news",
    "Need help navigating campus?",
    "Tap the mic to start chatting!",
  ];
  const [currentPromptIndex, setCurrentPromptIndex] = useState(0);

  useEffect(() => {
    if (isConnected) return;
    const interval = setInterval(() => {
      setCurrentPromptIndex((prev) => (prev + 1) % STANDBY_PROMPTS.length);
    }, 4000);
    return () => clearInterval(interval);
  }, [isConnected]);

  // Weather State
  const [weather, setWeather] = useState<{ temp: number; icon: string } | null>(
    null,
  );

  const scrollAreaRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Scroll to bottom whenever messages or transcriptions change
    if (scrollAreaRef.current) {
      // Use requestAnimationFrame to let React paint the new bubbles first
      requestAnimationFrame(() => {
        if (scrollAreaRef.current) {
          scrollAreaRef.current.scrollTop = scrollAreaRef.current.scrollHeight;
        }
      });
    }
  }, [messages, transcriptions, stagingText]);

  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      setTime(
        now.toLocaleTimeString("en-US", {
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        }),
      );
      setDateStr(
        now.toLocaleDateString("en-US", {
          weekday: "long",
          month: "short",
          day: "numeric",
        }),
      );
    };
    updateTime();
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, []);

  // Track connection state transitions
  const wasConnectedRef = useRef(isConnected);

  // Auto-disconnect after 5 minutes of inactivity
  useEffect(() => {
    if (!isConnected) {
      if (wasConnectedRef.current) {
        // Transitioned from connected to disconnected
        setFocusedEvent(null);
        pendingEventRef.current = null;
      }
      wasConnectedRef.current = false;
      return;
    }
    wasConnectedRef.current = true;

    const timeoutId = setTimeout(
      () => {
        console.log("Disconnecting due to inactivity");
        end();
      },
      5 * 60 * 1000,
    ); // 5 minutes

    return () => clearTimeout(timeoutId);
  }, [isConnected, end, messages, transcriptions]);

  useEffect(() => {
    const fetchWeather = async () => {
      try {
        const res = await fetch(
          "https://api.open-meteo.com/v1/forecast?latitude=6.7951&longitude=79.9003&current_weather=true",
        );
        const data = await res.json();
        const code = data.current_weather.weathercode;
        let icon = "light_mode";
        if (code === 0) icon = "light_mode";
        else if (code === 1 || code === 2) icon = "partly_cloudy_day";
        else if (code === 3) icon = "cloud";
        else if (code === 45 || code === 48) icon = "foggy";
        else if (code >= 51 && code <= 65) icon = "rainy";
        else if (code >= 71 && code <= 77) icon = "weather_snow";
        else if (code >= 80 && code <= 82) icon = "rainy";
        else if (code >= 85 && code <= 86) icon = "weather_snow";
        else if (code >= 95) icon = "thunderstorm";

        setWeather({
          temp: Math.round(data.current_weather.temperature),
          icon,
        });
      } catch (err) {
        console.error("Failed to fetch weather", err);
      }
    };
    fetchWeather();
    const interval = setInterval(fetchWeather, 30 * 60 * 1000); // 30 mins
    return () => clearInterval(interval);
  }, []);

  // Fetch Facebook Posts
  useEffect(() => {
    const fetchPosts = async () => {
      try {
        const response = await fetch("/api/facebook");
        const data = await response.json();
        if (Array.isArray(data) && data.length > 0) {
          setFacebookPosts(data);
        }
      } catch (error) {
        console.error("Failed to fetch FB posts:", error);
      }
    };
    fetchPosts();
    // Refresh every 30 minutes
    const interval = setInterval(fetchPosts, 30 * 60 * 1000);
    return () => clearInterval(interval);
  }, []);

  // Slideshow Logic - only rotates when standby (disconnected)
  useEffect(() => {
    if (isConnected) return;
    if (fbPosts.length <= 1) return;
    const interval = setInterval(() => {
      setCurrentSlide((prev) => (prev + 1) % fbPosts.length);
    }, 8000); // 8 seconds per slide
    return () => clearInterval(interval);
  }, [fbPosts.length, isConnected]);

  // Swipe to change slides (refs avoid re-render on every touchmove)
  const touchStartRef = useRef<number | null>(null);
  const touchEndRef = useRef<number | null>(null);

  const minSwipeDistance = 50;

  const onTouchStart = (e: React.TouchEvent) => {
    touchEndRef.current = null;
    touchStartRef.current = e.targetTouches[0].clientX;
  };

  const onTouchMove = (e: React.TouchEvent) => {
    touchEndRef.current = e.targetTouches[0].clientX;
  };

  const onTouchEnd = () => {
    const touchStart = touchStartRef.current;
    const touchEnd = touchEndRef.current;
    if (touchStart == null || touchEnd == null) return;
    const distance = touchStart - touchEnd;
    const isLeftSwipe = distance > minSwipeDistance;
    const isRightSwipe = distance < -minSwipeDistance;

    if (isLeftSwipe && fbPosts.length > 0) {
      setCurrentSlide((prev) => (prev + 1) % fbPosts.length);
    }
    if (isRightSwipe && fbPosts.length > 0) {
      setCurrentSlide((prev) => (prev - 1 + fbPosts.length) % fbPosts.length);
    }
  };

  return (
    <div
      className="kiosk-mode relative text-on-background w-full h-screen overflow-hidden flex flex-col select-none bg-[#f4f7fb] dark:bg-black"
      style={{ fontFamily: "Inter, sans-serif", touchAction: "manipulation" }}
    >
      {/* Subtle Material You Premium Background */}
      <div className="absolute inset-0 -z-20 pointer-events-none overflow-hidden">
        {/* Ambient Glowing Blobs - Hidden in true dark mode */}
        <div className="absolute -top-[20%] -left-[10%] w-[60%] h-[60%] bg-primary-container/40 dark:hidden rounded-full kiosk-ambient-blur pointer-events-none" />
        <div className="absolute -bottom-[20%] -right-[10%] w-[60%] h-[60%] bg-tertiary-container/40 dark:hidden rounded-full kiosk-ambient-blur pointer-events-none" />
      </div>

      {/* Main Content Wrapper (must be above background) */}
      <div className="relative z-10 w-full h-full flex flex-col">
        {/* Top App Bar */}
        <header className="bg-transparent flex-shrink-0 w-full flex justify-between items-center px-6 h-[60px] z-20">
          <div className="text-[26px] font-black tracking-[-0.04em] text-black dark:text-white">
            NEma
          </div>
          <div className="flex items-center gap-4">
            <button
              onClick={() => setIsUploadModalOpen(true)}
              className="border border-black/15 dark:border-white/15 hover:bg-black/5 dark:hover:bg-white/5 transition-colors rounded-full px-5 py-2 flex items-center justify-center text-black dark:text-white text-[13px] font-semibold gap-2 active:scale-95"
            >
              <UploadCloud className="w-4 h-4" />
              Upload Poster
            </button>
            {isConnected && (
              <div className="border border-black/15 dark:border-white/15 text-black/60 dark:text-white/60 px-3 py-1.5 rounded-full text-xs font-semibold flex items-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-black/40 dark:bg-white/40 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-black/70 dark:bg-white/70"></span>
                </span>
                Connected
              </div>
            )}
            <button
              onClick={() => {
                 const states = [null, 'where-to', 'chat', 'mic', 'news'] as any;
                 const nextIdx = (states.indexOf(glowingSection) + 1) % states.length;
                 setGlowingSection(states[nextIdx]);
              }}
              className="border border-black/15 dark:border-white/15 hover:bg-black/5 dark:hover:bg-white/5 transition-colors rounded-full px-5 py-2 flex items-center justify-center text-black dark:text-white text-[13px] font-semibold gap-2 active:scale-95"
            >
              Glow: {glowingSection || 'Off'}
            </button>
            <button
              onClick={() => setIsColorModalOpen(true)}
              className="border border-black/15 dark:border-white/15 hover:bg-black/5 dark:hover:bg-white/5 transition-colors rounded-full p-2.5 flex items-center justify-center text-black dark:text-white active:scale-95"
              aria-label="Open settings"
            >
              <Settings className="w-[22px] h-[22px]" />
            </button>
            <ThemeToggle />
          </div>
        </header>

        {/* Main Content Area - Bento Grid */}
        <main className="flex-1 px-3 pt-0 pb-3 min-h-0 flex flex-col">
          <div className="flex gap-3 flex-1 min-h-0 pb-1">
            {/* Left Column: Clock & Faculty News — expands when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0, width: focusedEvent ? "65%" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0 min-w-0"
            >
              {/* Clock & Weather Card */}
              {!focusedEvent && (
                <div className="kiosk-clock-card bg-[#d3e3fd] text-[#041e49] dark:bg-[#0a0a0a] dark:text-white rounded-[32px] px-4 py-10 pt-12 flex flex-col items-center justify-center relative flex-shrink-0 transition-transform hover:scale-[1.02]">
                {weather ? (
                  <div className="absolute top-3 right-4 flex items-center opacity-80 text-primary">
                    <span className="material-symbols-outlined text-[24px] fill-current">
                      {weather.icon}
                    </span>
                  </div>
                ) : (
                  <span className="material-symbols-outlined absolute top-3 right-4 text-[24px] opacity-20 fill-current">
                    light_mode
                  </span>
                )}
                <div className="w-full px-1">
                  <div className="kiosk-clock-time">{time || "10:42"}</div>
                </div>
                <div className="kiosk-clock-date mt-1 font-semibold opacity-80 w-full px-1">
                  {dateStr || "Thursday, June 4"}
                </div>
                </div>
              )}

              <div className="relative h-full flex flex-col min-h-0">
                <SiriGlow active={glowingSection === 'news'} />
                <div className={`z-10 rounded-[32px] h-full flex flex-col min-h-0 overflow-hidden relative ${focusedEvent ? "bg-[#f0f4f9] dark:bg-[#121212]" : "bg-[#ffe7e3] dark:bg-[#050505]"}`}>
                {focusedEvent ? (
                  /* Full poster view */
                  <>
                    {/* Poster image fills top */}
                    <div className="relative flex-1 min-h-0">
                      <img
                        src={focusedEvent.full_picture}
                        alt={focusedEvent.message}
                        className="w-full h-full object-contain bg-black/5 dark:bg-black/40"
                      />
                      {/* Back button */}
                      <button
                        onClick={() => setFocusedEvent(null)}
                        className="absolute top-3 left-3 z-10 bg-black/50 hover:bg-black/70 text-white rounded-full px-3 py-1.5 text-[12px] font-bold flex items-center gap-1.5 transition-colors backdrop-blur-sm"
                      >
                        <span className="material-symbols-outlined text-[16px]">
                          arrow_back
                        </span>
                        Back
                      </button>
                    </div>
                    {/* Event details below image */}
                    <div className="flex-shrink-0 p-5 bg-white/60 dark:bg-black/40 backdrop-blur-lg border-t border-white/20 dark:border-white/5">
                      <p className="text-on-surface font-semibold text-[16px] leading-snug mb-1">
                        {focusedEvent.message}
                      </p>
                      {focusedEvent.description && (
                        <p className="text-on-surface/75 text-[13px] leading-relaxed line-clamp-3 mb-3">
                          {focusedEvent.description}
                        </p>
                      )}
                      <div className="flex flex-wrap gap-2">
                        {focusedEvent.extracted_date && (
                          <span className="bg-primary/10 text-primary border border-primary/20 px-2.5 py-1 rounded-full text-[11px] font-semibold">
                            📅 {focusedEvent.extracted_date}
                          </span>
                        )}
                        {focusedEvent.extracted_location && (
                          <span className="bg-primary/10 text-primary border border-primary/20 px-2.5 py-1 rounded-full text-[11px] font-semibold">
                            📍 {focusedEvent.extracted_location}
                          </span>
                        )}
                      </div>
                    </div>
                  </>
                ) : (
                  /* Normal news list */
                  <>
                    {/* Header */}
                    <div className="flex-shrink-0 px-5 pt-5 pb-3">
                      <h2 className="text-[24px] leading-[32px] tracking-[-0.02em] text-on-surface font-bold flex-shrink-0">
                        Faculty News
                      </h2>
                    </div>

                    {/* Category color map */}
                    <div className="flex-1 flex flex-col gap-3 overflow-hidden px-4 pb-5">
                      {localPosts.slice(0, 3).map((post, i) => {
                        const categoryColors: Record<string, string> = {
                          events: "bg-violet-500",
                          competitions: "bg-orange-500",
                          posts: "bg-teal-500",
                        };
                        const accent =
                          categoryColors[post.category] ||
                          "bg-primary";
                        return (
                          <button
                            key={post.id}
                            className="w-full text-left rounded-2xl overflow-hidden cursor-pointer active:scale-[0.97] transition-transform focus:outline-none"
                            onClick={() => handleNewsClick(post)}
                          >
                            {/* Card: image thumbnail + text side by side */}
                            <div className="flex bg-white/50 dark:bg-black/20 hover:bg-white/70 dark:hover:bg-black/40 border border-white/20 dark:border-white/5 backdrop-blur-sm transition-all duration-200">
                              {/* Thumbnail */}
                              <div className="relative w-[80px] flex-shrink-0 overflow-hidden">
                                <img
                                  src={post.full_picture}
                                  alt={post.message}
                                  className="w-full h-full object-cover min-h-[80px]"
                                />
                                {/* Gradient accent bar on left edge */}
                                <div
                                  className={`absolute inset-y-0 left-0 w-1 ${accent}`}
                                />
                              </div>
                              {/* Text content */}
                              <div className="flex-1 p-3 min-w-0">
                                <div className="flex items-center gap-1.5 mb-1.5">
                                  <span
                                    className={`inline-block w-2 h-2 rounded-full ${accent} flex-shrink-0`}
                                  />
                                  <span className="text-[10px] font-bold uppercase tracking-[0.12em] opacity-70">
                                    {post.category.replace(/s$/, "")}
                                  </span>
                                  {post.extracted_date && (
                                    <span className="ml-auto text-[10px] font-semibold opacity-50 flex-shrink-0">
                                      {post.extracted_date.substring(0, 6)}
                                    </span>
                                  )}
                                </div>
                                <p className="text-[14px] font-semibold text-on-surface leading-tight line-clamp-2">
                                  {post.message}
                                </p>
                                {post.description && (
                                  <p className="text-[12px] text-on-surface/60 mt-1 line-clamp-1">
                                    {post.description}
                                  </p>
                                )}
                              </div>
                            </div>
                          </button>
                        );
                      })}
                      {localPosts.length === 0 && (
                        <div className="flex-1 flex flex-col items-center justify-center gap-3 opacity-50">
                          <span className="material-symbols-outlined text-5xl">
                            newspaper
                          </span>
                          <p className="text-[14px] italic text-center">
                            No recent news.
                            <br />
                            Upload a poster to get started.
                          </p>
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
              </div>
            </motion.div>

            {/* Middle Column: Events Carousel & Microphone — flex-1 fills freed space */}
            <motion.div layout initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ type: "spring", stiffness: 300, damping: 30, delay: 0.1 }} className="flex-1 h-full min-h-0 flex flex-col gap-2 min-w-0">
              <div className="bg-[#e6f4ea] dark:bg-[#050505] rounded-[32px] flex-1 overflow-hidden relative flex flex-col min-h-0">
                {navData ? (
                  <div className="flex-1 flex flex-col relative h-full bg-surface-container rounded-[32px] overflow-hidden">
                    <div className="absolute top-4 left-6 right-6 z-20 flex justify-between items-center bg-surface-container-highest border-none rounded-full px-6 py-3">
                      <div className="flex items-center gap-3 text-on-surface">
                        <div className="w-3 h-3 bg-red-500 rounded-full animate-pulse" />
                        <span className="text-white text-lg font-bold">
                          Navigating to: {navData.destination}
                        </span>
                      </div>
                      <button
                        onClick={() => setNavData(null)}
                        className="text-gray-400 hover:text-white text-2xl font-bold transition-colors"
                      >
                        &times;
                      </button>
                    </div>
                    <Suspense
                      fallback={
                        <div className="flex h-full items-center justify-center text-white/50 animate-pulse">
                          Loading 3D Map...
                        </div>
                      }
                    >
                      <NavigationMap
                        path={navData.path}
                        nodes={navData.nodes}
                        buildings={navData.buildings}
                        destination={navData.destination}
                        inline={true}
                        onClose={() => setNavData(null)}
                      />
                    </Suspense>
                  </div>
                ) : (isConnected && !isAgentInitializing) ? (
                  <div className="flex-1 flex flex-col relative h-full bg-transparent pt-4">

                    <ScrollArea
                      ref={scrollAreaRef}
                      className="flex-1 px-4 relative z-10"
                    >
                      <ChatTranscript
                        messages={messages}
                        transcriptions={transcriptions}
                        stagingText={stagingText}
                        isLoading={false}
                        className="space-y-4 pb-4"
                      />
                    </ScrollArea>
                  </div>
                ) : (
                  <div
                    className="absolute inset-0 z-0 flex flex-col"
                    onTouchStart={onTouchStart}
                    onTouchMove={onTouchMove}
                    onTouchEnd={onTouchEnd}
                  >
                    <div className="absolute inset-0 z-0 bg-secondary-container bg-black">
                      {fbPosts.length > 0 ? (
                        fbPosts.map((post, index) => (
                          <img
                            key={post.id}
                            alt="Facebook Post"
                            className={`absolute inset-0 w-full h-full object-cover transition-all duration-1000 ease-in-out ${index === currentSlide ? "opacity-100 scale-100 blur-none" : "opacity-0 scale-[1.05] blur-[4px]"}`}
                            src={post.full_picture}
                          />
                        ))
                      ) : (
                        <div className="absolute inset-0 w-full h-full bg-surface-variant/80 animate-breathe"></div>
                      )}
                    </div>
                    <div className="relative z-10 p-6 flex flex-col h-full bg-gradient-to-t from-black/80 via-black/30 to-transparent text-white">
                      <div className="flex-1 w-full relative">
                        {fbPosts.length > 0 ? (
                          fbPosts.map((post, index) => (
                            <div
                              key={post.id}
                              className={`absolute bottom-2 left-0 w-full flex flex-col justify-end transition-all duration-1000 ease-in-out ${
                                index === currentSlide
                                  ? "opacity-100 translate-y-0 blur-none scale-100 pointer-events-auto"
                                  : "opacity-0 translate-y-3 blur-[4px] scale-[0.98] pointer-events-none"
                              }`}
                            >
                              <h3 className="text-[20px] font-normal leading-tight mb-2 line-clamp-3 opacity-90">
                                {post.message}
                              </h3>

                              {post.description && (
                                <p className="text-[14px] opacity-80 mb-2 line-clamp-2">
                                  {post.description}
                                </p>
                              )}

                              {post.extracted_date && (
                                <p className="text-[13px] font-semibold text-indigo-300 mb-1">
                                  📅 {post.extracted_date}{" "}
                                  {post.extracted_time
                                    ? `• ${post.extracted_time}`
                                    : ""}
                                </p>
                              )}

                              {post.extracted_location && (
                                <p className="text-[13px] font-semibold text-purple-300 mb-3">
                                  📍 {post.extracted_location}
                                </p>
                              )}

                              <p className="text-[11px] opacity-60">
                                Posted on:{" "}
                                {new Date(
                                  post.created_time,
                                ).toLocaleDateString()}
                              </p>
                            </div>
                          ))
                        ) : (
                          <div className="space-y-3 animate-breathe opacity-60 absolute bottom-2 left-0 w-full">
                            <div className="h-7 bg-white/30 rounded-md w-3/4"></div>
                            <div className="h-5 bg-white/30 rounded-md w-1/2"></div>
                            <div className="h-4 bg-white/30 rounded-md w-1/4 mt-4"></div>
                          </div>
                        )}
                      </div>
                    </div>
                    {/* Carousel Indicators */}
                    {fbPosts.length > 1 && (
                      <div className="absolute bottom-4 left-0 right-0 flex justify-center gap-2 z-20">
                        {fbPosts.map((_, idx) => (
                          <div
                            key={idx}
                            onClick={() => setCurrentSlide(idx)}
                            className={`w-2.5 h-2.5 rounded-full cursor-pointer transition-all ${idx === currentSlide ? "bg-on-secondary scale-110" : "bg-on-secondary/50 hover:bg-on-secondary/80 scale-100"}`}
                          ></div>
                        ))}
                      </div>
                    )}
                    {/* Facebook Logo Watermark */}
                    {fbPosts.length > 0 && !fbPosts[currentSlide]?.isLocal && (
                      <div className="absolute bottom-4 right-4 z-20 text-[#1877F2] bg-white rounded-full p-[2px] flex items-center justify-center pointer-events-none">
                        <svg
                          viewBox="0 0 24 24"
                          fill="currentColor"
                          className="w-7 h-7"
                        >
                          <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.469h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.469h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z" />
                        </svg>
                      </div>
                    )}
                  </div>
                )}
              </div>
              {/* Microphone Action Area */}
              <div className="relative flex-shrink-0 min-h-[112px] h-auto rounded-[32px] w-full">
                <SiriGlow active={glowingSection === 'mic' || isThinking} />
                <div
                  className={`z-10 h-full py-4 flex items-center justify-center rounded-[32px] relative px-4 overflow-hidden transition-all duration-300 ${isConnected ? "bg-primary-container dark:bg-primary-container" : "bg-[#f0f4f9] dark:bg-[#1a2235]"}`}
                  style={{
                    boxShadow: isConnected
                      ? `0 0 ${maxVolume * 40}px rgba(var(--tw-colors-primary-rgb), ${maxVolume * 0.3})`
                      : undefined,
                  }}
                >
                <div className="w-full flex justify-center items-center text-center font-extrabold text-black dark:text-white tracking-tight leading-[1.2] min-h-[64px] relative z-10 pl-4 pr-20">
                  {!isConnected ? (
                    <div className="relative w-full overflow-hidden flex items-center justify-center h-full min-h-[64px]">
                      {STANDBY_PROMPTS.map((prompt, index) => (
                        <div
                          key={index}
                          className={`absolute inset-0 flex items-center justify-center text-center transition-all duration-1000 ease-in-out text-[28px] ${
                            currentPromptIndex === index
                              ? "opacity-100 translate-y-0 blur-none scale-100"
                              : "opacity-0 translate-y-3 blur-[4px] scale-[0.98] pointer-events-none"
                          }`}
                        >
                          {prompt}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="w-full flex justify-center break-words leading-[1.2] max-w-xl text-center text-[21px]">
                      {stagingText || ""}
                    </div>
                  )}
                </div>
                <div className="absolute right-4 z-10 flex items-center justify-center">
                  {/* Premium Voice Amplitude Halo — only when connected */}
                  {isConnected && (
                    <div
                      className="absolute inset-0 rounded-full blur-[12px] pointer-events-none transition-all duration-[50ms] ease-linear bg-primary/40 dark:bg-white/30"
                      style={{
                        transform: `scale(${1 + maxVolume * 1.2})`,
                        opacity: Math.max(0.2, pulseOpacity ?? 0),
                      }}
                    />
                  )}
                  {/* Gemini morphing shape replaces mic button while connecting */}
                  <GeminiMorphButton
                    isAnimating={isConnecting}
                    isConnected={isConnected}
                    onClick={handleMicClick}
                  />
                </div>
              </div>
              </div>
            </motion.div>

            {/* Right Column: Navigation — collapses when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: focusedEvent ? 0 : 1, y: 0, width: focusedEvent ? "0px" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30, delay: 0.2 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >
              {/* Where to? Card — with embedded 3D map (Material Secondary Tint) */}
              <div className="relative flex-1 flex flex-col min-h-0">
                <SiriGlow active={glowingSection === 'where-to'} />
                <div className="z-10 bg-[#f3edf7] dark:bg-[#050505] rounded-[32px] p-5 flex-1 flex flex-col relative overflow-hidden min-h-0">
                <h2 className="text-[24px] leading-[32px] tracking-[-0.02em] text-on-surface mb-2 font-bold flex-shrink-0">
                  Where to?
                </h2>

                {/* Embedded 3D Campus Map */}
                <div className="flex-1 min-h-0 rounded-[1.5rem] overflow-hidden mb-4 bg-surface-container border-none relative">
                  <Suspense
                    fallback={
                      <LoadingOverlay label="Loading map..." />
                    }
                  >
                    <CampusMapEmbed mapData={mapData} />
                  </Suspense>
                </div>

                {/* Room buttons */}
                <div className="flex flex-col gap-2 w-full flex-shrink-0">
                  {mapRooms.length > 0 ? (
                    mapRooms.slice(0, 3).map((roomNode, i) => (
                      <button
                        key={roomNode.id}
                        onClick={() => {
                          if (!isConnected) {
                            startSession();
                          }
                          setTimeout(
                            () => {
                              if (room) {
                                const payload = JSON.stringify({
                                  type: "event_focus",
                                  event: {
                                    title: roomNode.label,
                                    message: `Please give me directions to ${roomNode.label}`,
                                    category: "navigation",
                                  },
                                });
                                try {
                                  room.localParticipant.publishData(
                                    new TextEncoder().encode(payload),
                                    { reliable: true },
                                  );
                                } catch (e) {
                                  console.error(e);
                                }
                              }
                            },
                            isConnected ? 100 : 3000,
                          );
                        }}
                        className="bg-white/50 dark:bg-black/20 hover:bg-white/80 dark:hover:bg-black/40 text-on-surface border border-outline-variant/30 rounded-2xl h-[48px] w-full text-[14px] flex items-center justify-start px-5 gap-3 transition-all active:scale-[0.98] font-bold flex-shrink-0"
                      >
                        <span className="material-symbols-outlined text-[20px] opacity-70">
                          {i === 0
                            ? "school"
                            : i === 1
                              ? "apartment"
                              : "meeting_room"}
                        </span>
                        <span className="truncate capitalize">{roomNode.label.toLowerCase()}</span>
                      </button>
                    ))
                  ) : (
                    <>
                      <button className="bg-primary/10 text-primary border border-primary/20 rounded-2xl h-[48px] w-full text-[14px] flex items-center justify-start px-5 gap-3 transition-all active:scale-[0.98] font-semibold flex-shrink-0">
                        <span className="material-symbols-outlined text-[20px] opacity-80">
                          school
                        </span>
                        <span className="truncate">Dean's Office</span>
                      </button>
                      <button className="bg-transparent text-on-surface border border-transparent hover:bg-black/5 dark:hover:bg-white/5 hover:border-black/10 dark:hover:border-white/10 rounded-2xl h-[48px] w-full text-[14px] flex items-center justify-start px-5 gap-3 transition-all active:scale-[0.98] font-semibold flex-shrink-0">
                        <span className="material-symbols-outlined text-[20px] opacity-80">
                          apartment
                        </span>
                        <span className="truncate">Main Hall</span>
                      </button>
                    </>
                  )}
                </div>
              </div>
              </div>
            </motion.div>
          </div>
        </main>

        {/* Upload Poster QR Modal */}
        {isUploadModalOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm animate-in fade-in duration-200">
            <div className="bg-surface text-on-surface p-8 rounded-3xl max-w-md w-full relative animate-in zoom-in-95 duration-200">
              <button
                onClick={() => setIsUploadModalOpen(false)}
                className="absolute top-4 right-4 text-on-surface-variant hover:text-on-surface bg-surface-variant/50 hover:bg-surface-variant p-2 rounded-full transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
              <div className="flex flex-col items-center text-center space-y-6">
                <div className="bg-primary/10 p-4 rounded-full">
                  <UploadCloud className="w-8 h-8 text-primary" />
                </div>
                <div>
                  <h2 className="text-2xl font-bold mb-2">Upload a Poster</h2>
                  <p className="text-on-surface-variant">
                    Scan this QR code with your phone to quickly upload an event
                    poster to the Kiosk.
                  </p>
                </div>
                <div className="bg-white p-4 rounded-2xl">
                  <QRCodeSVG value={qrUrl} size={200} />
                </div>
                <p className="text-sm font-medium opacity-60">
                  or visit
                  <br />
                  <span className="text-primary">{qrUrl}</span>
                </p>
              </div>
            </div>
          </div>
        )}

        {isColorModalOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm animate-in fade-in duration-200">
            <div className="bg-surface text-on-surface p-8 rounded-3xl max-w-md w-full relative animate-in zoom-in-95 duration-200">
              <button
                onClick={() => setIsColorModalOpen(false)}
                className="absolute top-4 right-4 text-on-surface-variant hover:text-on-surface bg-surface-variant/50 hover:bg-surface-variant p-2 rounded-full transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
              <div className="flex flex-col items-center text-center space-y-6">
                <div>
                  <h2 className="text-2xl font-bold mb-2">Customize Eyes</h2>
                  <p className="text-on-surface-variant">
                    Select a color to change the robot's eye color and UI theme.
                  </p>
                </div>
                <div className="grid grid-cols-3 gap-4 w-full">
                  {[
                    { name: "White", eyeTheme: "white", uiTheme: "", color: "bg-white border-gray-200" },
                    { name: "Pistachio", eyeTheme: "pistachio", uiTheme: "pistachio", color: "bg-[#93c572]" },
                    { name: "Coral", eyeTheme: "coral", uiTheme: "coral", color: "bg-[#ff7f50]" },
                    { name: "Red", eyeTheme: "red", uiTheme: "", color: "bg-red-500 border-red-600" },
                    { name: "Green", eyeTheme: "green", uiTheme: "", color: "bg-green-500 border-green-600" },
                    { name: "Blue", eyeTheme: "blue", uiTheme: "", color: "bg-blue-500 border-blue-600" },
                    { name: "Yellow", eyeTheme: "yellow", uiTheme: "", color: "bg-yellow-400 border-yellow-500" },
                    { name: "Cyan", eyeTheme: "cyan", uiTheme: "", color: "bg-cyan-400 border-cyan-500" },
                    { name: "Magenta", eyeTheme: "purple", uiTheme: "", color: "bg-fuchsia-500 border-fuchsia-600" },
                  ].map((c) => (
                    <button
                      key={c.name}
                      onClick={() => {
                        applyEyeColor(c.eyeTheme, c.uiTheme);
                        setIsColorModalOpen(false);
                      }}
                      className={`h-12 rounded-xl flex items-center justify-center font-bold transition-transform active:scale-95 border ${c.color} ${c.name === 'White' || c.name === 'Yellow' || c.name === 'Cyan' ? 'text-black' : 'text-white'}`}
                    >
                      {c.name}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Listens for image messages to show popup posters (ignores navigation to let KioskView handle it inline) */}
        <ImageDisplay ignoreNavigation={true} />
      </div>

    </div>
  );
}
