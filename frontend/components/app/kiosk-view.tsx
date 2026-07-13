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
  useLivekitOffline,
  Track
} from "@/hooks/useLivekitOffline";
import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  Suspense,
} from "react";
import { motion, AnimatePresence, useScroll, useTransform, useMotionTemplate } from "motion/react";
import { ChatTranscript } from "@/components/app/chat-transcript";
import { ScrollArea } from "@/components/livekit/scroll-area/scroll-area";
import { ThemeToggle } from "@/components/app/theme-toggle";
import { QRCodeSVG } from "qrcode.react";
import { UploadCloud, X, Settings, Palette } from "lucide-react";
import dynamic from "next/dynamic";
import LoadingOverlay from "@/components/ui/LoadingOverlay";
import { GeminiMorphButton } from "@/components/ui/GeminiMorphButton";
import { SiriGlow } from "@/components/ui/SiriGlow";
import { ImageDisplay } from "@/components/app/image-display";
import ReactMarkdown from "react-markdown";

// Lazy load 3D map to avoid SSR issues with Three.js
const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

function FocusedEventView({ focusedEvent, onClose }: { focusedEvent: any; onClose: () => void }) {
  const posterScrollRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({ container: posterScrollRef });
  
  // Apple Music style blur effect as text scrolls over it
  const blurAmount = useTransform(scrollYProgress, [0, 0.4], [0, 30]);
  const imageOpacity = useTransform(scrollYProgress, [0, 0.4], [1, 0.4]);
  const blurFilter = useMotionTemplate`blur(${blurAmount}px)`;

  return (
    <motion.div 
      drag="y" 
      dragConstraints={{ top: 0, bottom: 0 }} 
      dragElastic={0.2} 
      onDragEnd={(e, info) => { if (info.offset.y > 100) onClose(); }}
      ref={posterScrollRef} 
      className="relative w-full h-full overflow-y-auto [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none] touch-pan-y cursor-grab active:cursor-grabbing"
    >
      {/* Sticky Back Button (Zero-height so it doesn't push content down) */}
      <div className="sticky top-0 z-30 pointer-events-none w-full h-0">
        <div className="p-3 flex justify-start">
          <button
            onClick={onClose}
            className="pointer-events-auto bg-black/50 hover:bg-black/70 text-white rounded-full px-3 py-1.5 text-[12px] font-bold flex items-center gap-1.5 transition-colors backdrop-blur-sm shadow-md"
          >
            <span className="material-symbols-outlined text-[16px]">
              arrow_back
            </span>
            Back
          </button>
        </div>
      </div>

      {/* Sticky Image Container (Blur Effect on Scroll) */}
      <motion.div 
        className="sticky top-0 w-full h-[75vh] flex flex-col justify-center bg-black/5 dark:bg-black/40 overflow-hidden -z-10"
        style={{
          filter: blurFilter,
          opacity: imageOpacity
        }}
      >
        {/* Blurred ambient background to fill empty space */}
        <img
          src={focusedEvent.full_picture}
          alt=""
          className="absolute inset-0 w-full h-full object-cover blur-3xl opacity-50 scale-110"
        />
        {/* Uncropped foreground poster */}
        <div className="relative z-10 w-full h-full p-4 flex items-center justify-center drop-shadow-[0_15px_40px_rgba(0,0,0,0.4)]">
          <img
            src={focusedEvent.full_picture}
            alt={focusedEvent.message}
            className="max-w-full max-h-full object-contain rounded-[24px]"
          />
        </div>
      </motion.div>

      {/* Scrollable Event Details */}
      <div className="relative z-20 p-6 bg-white/95 dark:bg-[#202020]/95 backdrop-blur-2xl min-h-[50vh] shadow-[0_-15px_40px_rgba(0,0,0,0.15)] rounded-t-[32px] -mt-6">
        <p className="text-on-surface font-semibold text-[22px] leading-snug mb-3">
          {focusedEvent.message}
        </p>
        <div className="flex flex-wrap gap-2">
          {focusedEvent.extracted_date && !focusedEvent.extracted_date.includes("null") && (
            <span className="bg-primary/10 text-primary border border-primary/20 px-3 py-1.5 rounded-full text-[12px] font-semibold">
              📅 {focusedEvent.extracted_date}
            </span>
          )}
          {focusedEvent.extracted_location && !focusedEvent.extracted_location.includes("null") && (
            <span className="bg-primary/10 text-primary border border-primary/20 px-3 py-1.5 rounded-full text-[12px] font-semibold">
              📍 {focusedEvent.extracted_location}
            </span>
          )}
        </div>
      </div>
    </motion.div>
  );
}

export function KioskView() {
  const [glowingSection, setGlowingSection] = useState<'where-to' | 'chat' | 'mic' | 'news' | null>(null);
  const session = useSessionContext();
  const { isConnected, start, end } = session;
  const { messages } = useSessionMessages(session);
  const room = useRoomContext();
  const bb = useLivekitOffline();

  // Focused event state — set when a news card is tapped
  const [focusedEvent, setFocusedEvent] = useState<any | null>(null);
  const pendingEventRef = useRef<any | null>(null);
  const transcriptions = useTranscriptions();
  const [navData, setNavData] = useState<any | null>(null);
  const [isMapExpanded, setIsMapExpanded] = useState(false);
  const isNavigating = navData !== null;
  const pendingNavigateRef = useRef<string | null>(null);

  // Typewriter effect to simulate real-time streaming for offline-chunk text
  const useTypingEffect = (text: string, isActive: boolean, speedMs: number = 80) => {
    const [displayedText, setDisplayedText] = useState("");

    useEffect(() => {
      if (!isActive || !text || text === "undefined") {
        setDisplayedText("");
        return;
      }

      const words = text.split(" ");
      let currentIndex = 0;
      setDisplayedText("");

      const interval = setInterval(() => {
        if (currentIndex < words.length) {
          const nextText = words.slice(0, currentIndex + 1).join(" ");
          setDisplayedText(nextText);
          currentIndex++;
        } else {
          clearInterval(interval);
        }
      }, speedMs);

      return () => clearInterval(interval);
    }, [text, isActive, speedMs]);

    return displayedText;
  };

  const sanitizeText = (txt: string) => {
    if (!txt) return "";
    return txt
      .split(" ")
      .filter((w) => {
        const clean = w.toLowerCase().replace(/[.,\/#!$%\^&\*;:{}=\-_`~()]/g, "");
        return clean !== "undefined" && clean !== "undefine" && clean !== "undefines";
      })
      .join(" ");
  };

  const userText = bb.user_text ? sanitizeText(bb.user_text) : "";
  const agentText = bb.agent_text ? sanitizeText(bb.agent_text) : "";

  const userTextFinal = userText || "Thinking...";
  const agentTextFinal = agentText || "Speaking...";

  const userTypingText = useTypingEffect(userText, isConnected, 50);
  const agentTypingText = useTypingEffect(agentTextFinal, bb.conv_state === "speaking", 380);

  // Auto-timeout: Close poster if idle for 45s
  useEffect(() => {
    if (!focusedEvent) return;
    const timer = setTimeout(() => {
      setFocusedEvent(null);
    }, 45000);
    return () => clearTimeout(timer);
  }, [focusedEvent]);

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

  const sendNavigateRequest = useCallback(
    (destination: string) => {
      if (!room) return;
      try {
        const payload = JSON.stringify({ type: "navigate_request", destination });
        room.localParticipant.publishData(new TextEncoder().encode(payload), {
          reliable: true,
        });
        console.log("📲 Sent navigate_request to agent:", destination);
      } catch (e) {
        console.error("Failed to publish navigate request:", e);
      }
    },
    [room],
  );

  // When connection established AND there's a pending event/navigation, send it
  useEffect(() => {
    if (isConnected) {
      if (pendingEventRef.current) {
        const ev = pendingEventRef.current;
        pendingEventRef.current = null;
        setTimeout(() => sendEventFocus(ev), 2500);
      }
      if (pendingNavigateRef.current) {
        const dest = pendingNavigateRef.current;
        pendingNavigateRef.current = null;
        setTimeout(() => sendNavigateRequest(dest), 2500);
      }
    }
  }, [isConnected, sendEventFocus, sendNavigateRequest]);

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
        await start();
      } else {
        sendEventFocus(post);
      }
    },
    [isConnected, start, sendEventFocus],
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
        setTimeout(() => reject(new Error("Connection timeout")), 15000)
      );
      await Promise.race([start(), timeoutPromise]);
    } catch (e) {
      console.error("Agent connection failed:", e);
      setIsConnecting(false);
    }
  }, [isConnected, start, end]);

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
  const [showColors, setShowColors] = useState(false);
  const [qrUrl, setQrUrl] = useState("");

  // 3D Map data — full world-coordinate nodes for the mini-map preview
  const [homeMapData, setHomeMapData] = useState<{ nodes: any[]; buildings: any; edges: any[] } | null>(null);

  useEffect(() => {
    fetch("/api/map?floor=floor_1")
      .then((res) => res.json())
      .then((data) => {
        const buildings = data.buildings || {};
        const edges = data.edges || [];
        const nodes = (data.nodes || []).map((n: any) => {
          const b = buildings[n.building] || { position: [0, 0, 0] };
          return {
            ...n,
            floor: "floor_1",
            world: [b.position[0] + n.x, 0, b.position[2] + n.z],
          };
        });
        setHomeMapData({ nodes, buildings, edges });
      })
      .catch(() => {});
  }, []);

  // All-floor locations for category modal
  const [locationsModalCategory, setLocationsModalCategory] = useState<string | null>(null);
  const [allLocations, setAllLocations] = useState<any[]>([]);
  const [filteredLocations, setFilteredLocations] = useState<any[]>([]);

  useEffect(() => {
    fetch("/api/locations")
      .then((res) => res.json())
      .then((data) => { if (data.locations) setAllLocations(data.locations); })
      .catch(() => {});
  }, []);

  const handleCategoryClick = (category: string, filterKeyword: string) => {
    setLocationsModalCategory(category);
    setFilteredLocations(
      allLocations.filter((loc) =>
        loc.label.toLowerCase().includes(filterKeyword.toLowerCase()),
      ),
    );
  };

  const [isNavLoading, setIsNavLoading] = useState(false);

  const handleNavigateToLocation = async (destination: string) => {
    setLocationsModalCategory(null);
    setIsMapExpanded(true);
    setIsNavLoading(true);
    try {
      // Directly call the navigate API — no voice agent required
      const res = await fetch(`/api/navigate?destination=${encodeURIComponent(destination)}`);
      const data = await res.json();
      if (data && data.path_coords) {
        setNavData({
          type: "navigation",
          destination: data.destination,
          path: data.path_coords,
          path_coords: data.path_coords,
          path_ids: data.path_ids || [],
          directions: data.directions || "",
          nodes: data.nodes || [],
          buildings: data.buildings || {},
        });
        // Also notify the agent (if connected) so it speaks the directions
        if (isConnected) {
          sendNavigateRequest(destination);
        }
      } else {
        console.error("Navigate API error:", data);
        setIsMapExpanded(false);
      }
    } catch (e) {
      console.error("Failed to fetch navigation:", e);
      setIsMapExpanded(false);
    } finally {
      setIsNavLoading(false);
    }
  };


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
    }, 2000);

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

  // Swipe to change slides
  const [touchStart, setTouchStart] = useState<number | null>(null);
  const [touchEnd, setTouchEnd] = useState<number | null>(null);

  const minSwipeDistance = 50;

  const onTouchStart = (e: React.TouchEvent) => {
    setTouchEnd(null);
    setTouchStart(e.targetTouches[0].clientX);
  };

  const onTouchMove = (e: React.TouchEvent) =>
    setTouchEnd(e.targetTouches[0].clientX);

  const onTouchEnd = () => {
    if (!touchStart || !touchEnd) return;
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
      className="relative text-on-background w-full h-screen overflow-hidden flex flex-col select-none bg-[#f4f7fb] dark:bg-black"
      style={{ fontFamily: "Inter, sans-serif" }}
    >
      {/* Subtle Material You Premium Background */}
      <div className="absolute inset-0 -z-20 pointer-events-none overflow-hidden">
        {/* Ambient Glowing Blobs - Hidden in true dark mode */}
        <div className="absolute -top-[20%] -left-[10%] w-[60%] h-[60%] bg-primary-container/40 dark:hidden rounded-full blur-[140px] pointer-events-none" />
        <div className="absolute -bottom-[20%] -right-[10%] w-[60%] h-[60%] bg-tertiary-container/40 dark:hidden rounded-full blur-[140px] pointer-events-none" />
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
            <div className="relative flex items-center">
              <AnimatePresence>
                {showColors && (
                  <motion.div
                    initial={{ width: 0, opacity: 0, marginRight: 0 }}
                    animate={{ width: "auto", opacity: 1, marginRight: 12 }}
                    exit={{ width: 0, opacity: 0, marginRight: 0 }}
                    className="flex items-center gap-1.5 bg-black/5 dark:bg-white/5 p-1.5 rounded-full overflow-hidden"
                  >
                    {[
                      { name: "White", theme: "", color: "bg-white" },
                      { name: "Pistachio", theme: "pistachio", color: "bg-[#93c572]" },
                      { name: "Coral", theme: "coral", color: "bg-[#ff7f50]" },
                      { name: "Blue", theme: "", color: "bg-blue-500" },
                      { name: "Cyan", theme: "", color: "bg-cyan-400" },
                      { name: "Magenta", theme: "", color: "bg-fuchsia-500" },
                    ].map((c) => (
                      <button
                        key={c.name}
                        onClick={() => {
                          if (c.theme) {
                            document.documentElement.setAttribute("data-pixel-theme", c.theme);
                          } else {
                            document.documentElement.removeAttribute("data-pixel-theme");
                          }
                          if (room) {
                            try {
                              room.localParticipant.publishData(
                                new TextEncoder().encode(JSON.stringify({
                                  type: "change_eye_color",
                                  color: c.name.toLowerCase()
                                })),
                                { reliable: true }
                              );
                            } catch (e) {
                              console.error("Failed to publish color data:", e);
                            }
                          }
                          setShowColors(false);
                        }}
                        className={`w-6 h-6 rounded-full flex-shrink-0 transition-transform hover:scale-110 active:scale-95 border border-black/10 dark:border-white/10 shadow-sm ${c.color}`}
                        aria-label={`Change color to ${c.name}`}
                      />
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>
              <button
                onClick={() => setShowColors(!showColors)}
                className={`border border-black/15 dark:border-white/15 hover:bg-black/5 dark:hover:bg-white/5 transition-colors rounded-full p-2.5 flex items-center justify-center text-black dark:text-white active:scale-95 ${showColors ? 'bg-black/10 dark:bg-white/10' : ''}`}
                aria-label="Toggle color palette"
              >
                <Palette className="w-[22px] h-[22px]" />
              </button>
            </div>
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
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >
              {/* Clock & Weather Card */}
              {!focusedEvent && (
                <div className="bg-[#d3e3fd] text-[#041e49] dark:bg-[#0a0a0a] dark:text-white rounded-[32px] px-8 py-10 pt-12 flex flex-col items-center justify-center relative overflow-hidden flex-shrink-0 transition-transform hover:scale-[1.02]">
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
                <div className="text-[64px] 2xl:text-[80px] leading-none tracking-[-0.04em] font-black whitespace-nowrap">
                  {time || "10:42"}
                </div>
                <div className="text-[16px] leading-[20px] mt-1 font-semibold opacity-80">
                  {dateStr || "Thursday, June 4"}
                </div>
                </div>
              )}

              <div className="relative h-full flex flex-col min-h-0">
                <SiriGlow active={glowingSection === 'news'} />
                <div className={`z-10 rounded-[32px] h-full flex flex-col min-h-0 overflow-hidden relative ${focusedEvent ? 'bg-[#f0f4f9] dark:bg-[#121212]' : 'bg-[#ffe7e3] dark:bg-[#050505]'}`}>
                {focusedEvent ? (
                  <FocusedEventView 
                    focusedEvent={focusedEvent} 
                    onClose={() => setFocusedEvent(null)} 
                  />
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
                {isNavLoading ? (
                  <div className="flex-1 flex items-center justify-center flex-col gap-4 bg-surface-container rounded-[32px]">
                    <span className="material-symbols-outlined animate-spin text-primary text-5xl">navigation</span>
                    <p className="text-on-surface-variant font-semibold text-[15px]">Calculating route...</p>
                  </div>
                ) : isNavigating ? (
                  <div className="flex-1 flex flex-col relative h-full bg-surface-container rounded-[32px] overflow-hidden">
                    <div className="absolute top-4 left-6 right-6 z-20 flex justify-between items-center bg-surface-container-highest border-none rounded-full px-6 py-3 shadow-md">
                      <div className="flex items-center gap-3 text-on-surface">
                        <div className="w-3 h-3 bg-primary rounded-full animate-pulse" />
                        <span className="text-on-surface text-lg font-bold">
                          Navigating to: {navData.destination}
                        </span>
                      </div>
                      <button
                        onClick={() => { setNavData(null); setIsMapExpanded(false); }}
                        className="text-on-surface-variant hover:text-on-surface text-2xl font-bold transition-colors"
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
                        path={navData.path_coords || navData.path}
                        path_ids={navData.path_ids}
                        nodes={navData.nodes}
                        buildings={navData.buildings}
                        destination={navData.destination}
                        isManualExpanded={true}
                        onClose={() => { setNavData(null); setIsMapExpanded(false); }}
                      />
                    </Suspense>
                  </div>
                ) : isMapExpanded ? (
                  <div className="flex-1 flex flex-col relative h-full bg-surface-container rounded-[32px] overflow-hidden">
                    <button
                      onClick={() => setIsMapExpanded(false)}
                      className="absolute top-6 right-6 z-20 w-12 h-12 flex items-center justify-center bg-surface-variant/90 backdrop-blur-sm border border-outline-variant/30 rounded-full text-on-surface-variant hover:text-on-surface hover:bg-surface-container-highest hover:scale-105 active:scale-95 text-3xl font-light transition-all shadow-md"
                      aria-label="Close Map"
                    >
                      &times;
                    </button>
                    <Suspense fallback={<div className="flex h-full items-center justify-center text-white/50 animate-pulse">Loading Map...</div>}>
                      {homeMapData && (
                        <NavigationMap
                          nodes={homeMapData.nodes}
                          buildings={homeMapData.buildings}
                          edges={homeMapData.edges}
                          isStandalone={true}
                          hideFloorSwitcher={false}
                          onNodeClick={handleNavigateToLocation}
                        />
                      )}
                    </Suspense>
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
              <div 
                onClick={handleMicClick}
                className="relative flex-shrink-0 min-h-[112px] h-auto rounded-[32px] w-full cursor-pointer hover:scale-[1.01] active:scale-[0.99] transition-all"
              >
                <SiriGlow active={glowingSection === 'mic' || isThinking} />
                <div
                  className={`z-10 h-full py-4 flex items-center justify-center rounded-[32px] relative px-4 overflow-hidden transition-all duration-300 ${isConnected ? "bg-primary-container dark:bg-primary-container" : "bg-[#f0f4f9] dark:bg-[#1a2235]"}`}
                  style={{
                    boxShadow: isConnected
                      ? `0 0 ${maxVolume * 40}px rgba(var(--tw-colors-primary-rgb), ${maxVolume * 0.3})`
                      : undefined,
                  }}
                >
                <div className="w-full flex justify-center items-center text-center font-extrabold text-black dark:text-white tracking-tight leading-[1.2] min-h-[80px] relative z-10 px-6 py-2">
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
                    <div className="flex flex-col items-center justify-center w-full max-w-xl">
                      {/* User Transcript Line (smaller, muted) */}
                      {userTypingText && (
                        <div className="text-[15px] text-[#6b7280] dark:text-[#9ca3af] font-medium opacity-90 max-w-full truncate break-words mb-1">
                          "{userTypingText}"
                        </div>
                      )}
                      
                      {/* Agent Response Line / Loading Indicator */}
                      <div className="w-full flex justify-center break-words leading-[1.2] max-w-xl text-center text-[21px] flex-wrap gap-x-1.5 justify-center">
                        {bb.conv_state === "thinking" && (
                          <div className="text-[#9ca3af] animate-pulse">
                            Thinking...
                          </div>
                        )}
                        {bb.conv_state === "speaking" && agentTypingText && (
                          agentTypingText.split(" ").map((word, i) => (
                            <motion.span
                              key={`agent-${i}-${word}`}
                              initial={{ opacity: 0, y: 4 }}
                              animate={{ opacity: 1, y: 0 }}
                              transition={{ duration: 0.2, ease: "easeOut" }}
                              className="inline-block"
                            >
                              {word}
                            </motion.span>
                          ))
                        )}
                        {bb.conv_state !== "thinking" && bb.conv_state !== "speaking" && agentText && (
                          agentText.split(" ").map((word, i) => (
                            <motion.span
                              key={`listen-${i}-${word}`}
                              initial={{ opacity: 0, y: 4 }}
                              animate={{ opacity: 1, y: 0 }}
                              transition={{ duration: 0.2, ease: "easeOut" }}
                              className="inline-block"
                            >
                              {word}
                            </motion.span>
                          ))
                        )}
                        {bb.conv_state !== "thinking" && bb.conv_state !== "speaking" && !agentText && (
                          <div className="text-[#3b82f6] animate-pulse">
                            Listening...
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </div>
              </div>
            </motion.div>

            {/* Right Column: Navigation — collapses when poster is focused or map is active */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: (focusedEvent || isMapExpanded || isNavigating) ? 0 : 1, y: 0, width: (focusedEvent || isMapExpanded || isNavigating) ? "0px" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30, delay: 0.2 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >
              {/* Where to? Card — multi-floor NavigationMap with category modal */}
              <div className="relative flex-1 flex flex-col min-h-0">
                <SiriGlow active={glowingSection === 'where-to'} />
                <div className="z-10 bg-[#f3edf7] dark:bg-[#050505] rounded-[32px] p-5 flex-1 flex flex-col relative overflow-hidden min-h-0">
                  <h2 className="text-[24px] leading-[32px] tracking-[-0.02em] text-on-surface mb-2 font-bold flex-shrink-0">
                    Where to?
                  </h2>

                  {/* Mini map preview — hidden when navigating or map expanded */}
                  <AnimatePresence>
                    {!isNavigating && !isMapExpanded && (
                      <motion.div
                        initial={{ opacity: 0, height: 0, marginBottom: 0 }}
                        animate={{ opacity: 1, height: 180, marginBottom: 8 }}
                        exit={{ opacity: 0, height: 0, marginBottom: 0 }}
                        className="w-full rounded-2xl overflow-hidden relative bg-black/10 mt-2 shadow-inner border border-outline/20 flex-shrink-0 cursor-pointer hover:ring-2 hover:ring-primary transition-all group"
                        onClick={() => setIsMapExpanded(true)}
                      >
                        {homeMapData ? (
                          <div className="absolute inset-0">
                            <Suspense fallback={<div className="w-full h-full flex items-center justify-center"><span className="material-symbols-outlined animate-spin text-primary opacity-50">refresh</span></div>}>
                              <NavigationMap
                                nodes={homeMapData.nodes}
                                buildings={homeMapData.buildings}
                                edges={homeMapData.edges}
                                isStandalone={true}
                                hideFloorSwitcher={true}
                                onNodeClick={handleNavigateToLocation}
                              />
                            </Suspense>
                            <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors flex items-center justify-center cursor-pointer">
                              <span className="material-symbols-outlined text-white opacity-0 group-hover:opacity-100 transition-opacity text-4xl drop-shadow-md">
                                open_in_full
                              </span>
                            </div>
                          </div>
                        ) : (
                          <div className="absolute inset-0 flex items-center justify-center">
                            <span className="material-symbols-outlined animate-spin text-primary opacity-50">refresh</span>
                          </div>
                        )}
                      </motion.div>
                    )}
                  </AnimatePresence>

                  {/* Category quick-nav buttons */}
                  <div className="flex flex-col gap-2 mt-auto w-full flex-shrink-0">
                    <button
                      onClick={() => handleCategoryClick("Lecture Halls", "lecture")}
                      className="bg-primary text-on-primary rounded-full h-[46px] w-full text-[15px] flex items-center justify-center gap-3 hover:bg-surface-tint transition-colors active:scale-95 shadow-md font-bold flex-shrink-0"
                    >
                      <span className="material-symbols-outlined text-xl">school</span>
                      Lecture Halls
                    </button>
                    <button
                      onClick={() => handleCategoryClick("Laboratory", "lab")}
                      className="bg-surface-variant text-on-surface-variant rounded-full h-[46px] w-full text-[15px] flex items-center justify-center gap-3 hover:bg-surface-container-highest transition-colors active:scale-95 shadow-sm border border-outline-variant font-bold flex-shrink-0"
                    >
                      <span className="material-symbols-outlined text-xl">science</span>
                      Laboratory
                    </button>
                    <button
                      onClick={() => handleCategoryClick("Offices & More", "office")}
                      className="bg-surface-variant text-on-surface-variant rounded-full h-[46px] w-full text-[15px] flex items-center justify-center gap-3 hover:bg-surface-container-highest transition-colors active:scale-95 shadow-sm border border-outline-variant font-bold flex-shrink-0"
                    >
                      <span className="material-symbols-outlined text-xl">apartment</span>
                      Offices &amp; More
                    </button>
                  </div>
                </div>
              </div>
            </motion.div>

          </div>
        </main>

        {/* Locations Category Modal */}
        <AnimatePresence>
          {locationsModalCategory && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm flex items-end justify-center"
              onClick={() => setLocationsModalCategory(null)}
            >
              <motion.div
                initial={{ y: "100%" }}
                animate={{ y: 0 }}
                exit={{ y: "100%" }}
                transition={{ type: "spring", stiffness: 300, damping: 30 }}
                className="bg-surface text-on-surface w-full max-w-xl rounded-t-3xl p-6 max-h-[70vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-xl font-bold">{locationsModalCategory}</h3>
                  <button
                    onClick={() => setLocationsModalCategory(null)}
                    className="text-on-surface-variant hover:text-on-surface bg-surface-variant/50 hover:bg-surface-variant p-2 rounded-full transition-colors"
                  >
                    <X className="w-5 h-5" />
                  </button>
                </div>
                {filteredLocations.length === 0 ? (
                  <p className="text-center text-on-surface-variant py-8">No rooms found in this category.</p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {filteredLocations.map((loc) => (
                      <button
                        key={loc.id}
                        onClick={() => handleNavigateToLocation(loc.label)}
                        className="bg-surface-container hover:bg-surface-container-highest text-on-surface border border-outline-variant/30 rounded-2xl h-[52px] w-full text-[15px] flex items-center justify-between px-5 gap-3 transition-all active:scale-[0.98] font-semibold"
                      >
                        <div className="flex items-center gap-3">
                          <span className="material-symbols-outlined text-[20px] text-primary opacity-70">navigation</span>
                          <span className="truncate">{loc.label}</span>
                        </div>
                        <span className="text-[11px] font-bold text-on-surface-variant opacity-60 flex-shrink-0">
                          {(loc.floor as string).replace("floor_", "Floor ")}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>

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



        {/* Listens for image messages to show popup posters (ignores navigation to let KioskView handle it inline) */}
        <ImageDisplay ignoreNavigation={true} />
      </div>

    </div>
  );
}
