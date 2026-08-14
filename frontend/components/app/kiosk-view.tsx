"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable @next/next/no-img-element */
import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
  Suspense,
} from "react";
import { motion, AnimatePresence } from "motion/react";
import { ThemeToggle } from "@/components/app/theme-toggle";
import { QRCodeSVG } from "qrcode.react";
import { UploadCloud, X, Settings } from "lucide-react";
import dynamic from "next/dynamic";
import { GeminiMorphButton } from "@/components/ui/GeminiMorphButton";
import { PopButton } from "@/components/ui/PopButton";
import { useNluAdapter } from "@/hooks/useNluAdapter";
import type { NluAction } from "@/hooks/useNluVoice";

/** Mount 3D map only when Maps mode / navigation needs it (no idle WebGL). */
const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

type KioskMode = "idle" | "events" | "maps" | "talk";

const DOCK_BTN =
  "min-h-[64px] min-w-[96px] px-4 text-[15px] font-bold flex flex-col items-center justify-center gap-0.5 rounded-2xl border border-transparent";
const DOCK_BTN_ACTIVE =
  "bg-[var(--kiosk-brand)] text-[var(--kiosk-brand-fg)]";
const DOCK_BTN_IDLE =
  "bg-transparent text-[var(--kiosk-text)]";
const CAT_BTN =
  "min-h-[96px] w-full rounded-2xl text-[24px] font-bold flex items-center justify-center gap-3 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)]";
const PANEL =
  "rounded-[28px] bg-[var(--kiosk-surface)] border border-[var(--kiosk-border)]";
const PAGE_PAD = "p-4";
const ICON_BTN =
  "w-20 h-20 rounded-full border border-[var(--kiosk-border)] flex items-center justify-center text-[var(--kiosk-muted)]";

const EVENT_CATEGORY_META: Record<
  string,
  { label: string; accent: string; chip: string }
> = {
  events: {
    label: "Event",
    accent: "bg-[var(--kiosk-cat-event)]",
    chip: "bg-[var(--kiosk-cat-event)]/20 text-[#EDE9FE]",
  },
  competitions: {
    label: "Competition",
    accent: "bg-[var(--kiosk-cat-comp)]",
    chip: "bg-[var(--kiosk-cat-comp)]/20 text-[#FFEDD5]",
  },
  posts: {
    label: "Announcement",
    accent: "bg-[var(--kiosk-cat-post)]",
    chip: "bg-[var(--kiosk-cat-post)]/20 text-[#CCFBF1]",
  },
};

function eventCategoryMeta(category?: string) {
  return (
    EVENT_CATEGORY_META[category || ""] || {
      label: "Campus",
      accent: "bg-neutral-500",
      chip: "bg-white/20 text-white",
    }
  );
}

export function KioskView() {
  return <KioskViewNlu />;
}

function KioskViewNlu() {
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

  useEffect(() => {
    const backendHost = typeof window !== "undefined" ? window.location.hostname : "localhost";
    fetch(`http://${backendHost}:8765/health`)
      .then((r) => r.json())
      .then((j) => console.log("[KioskNlu] NLU health:", j))
      .catch((e) => console.error("[KioskNlu] NLU health failed:", e));
  }, []);

  return (
    <KioskViewUI
      isConnected={nluAdapter.isConnected}
      startSession={startSession}
      end={end}
      messages={nluAdapter.messages}
      transcriptions={nluAdapter.transcriptions}
      agentState={nluAdapter.agentState}
      maxVolume={nluAdapter.maxVolume}
      lastAction={nluAdapter.lastAction}
      sendSimulatedVoice={nluAdapter.sendSimulatedVoice}
    />
  );
}



type KioskViewUIProps = {
  isConnected: boolean;
  startSession: () => Promise<void>;
  end: () => void;
  messages: any[];
  transcriptions: any[];
  agentState: string;
  maxVolume: number;
  lastAction: any | null;
  sendSimulatedVoice: (text: string) => void;
};

function KioskViewUI({
  isConnected,
  startSession,
  end,
  messages,
  transcriptions,
  agentState,
  maxVolume,
  lastAction,
  sendSimulatedVoice,
}: KioskViewUIProps) {
  const [mode, setMode] = useState<KioskMode>("idle");
  const [focusedEvent, setFocusedEvent] = useState<any | null>(null);
  const [eventCategory, setEventCategory] = useState<
    "events" | "competitions" | "posts" | null
  >(null);
  const pendingEventRef = useRef<any | null>(null);
  // Stable ref so early useEffects can call handlePosterTap before it is declared
  const handlePosterTapRef = useRef<(post: any) => void>(() => { });
  const processedActionRef = useRef<any>(null);
  const [navData, setNavData] = useState<any | null>(null);
  const [isConnecting, setIsConnecting] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [faceGreetingEnabled, setFaceGreetingEnabled] = useState(true);

  useEffect(() => {
    fetch("/api/face-greeting")
      .then((res) => res.json())
      .then((data) => setFaceGreetingEnabled(data.enabled))
      .catch(console.error);
  }, []);

  const toggleFaceGreeting = async () => {
    const newVal = !faceGreetingEnabled;
    setFaceGreetingEnabled(newVal);
    try {
      await fetch("/api/face-greeting", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: newVal }),
      });
    } catch (e) {
      console.error("Failed to toggle face greeting", e);
      setFaceGreetingEnabled(!newVal); // revert
    }
  };

  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [qrUrl, setQrUrl] = useState("");
  const [time, setTime] = useState("");
  const [pageVisible, setPageVisible] = useState(true);

  // Maps — loaded lazily on first Maps enter
  const [mapsReady, setMapsReady] = useState(false);
  const [allLocations, setAllLocations] = useState<any[]>([]);
  const [locationsModalCategory, setLocationsModalCategory] = useState<
    string | null
  >(null);
  const [filteredLocations, setFilteredLocations] = useState<any[]>([]);
  const [exploreMapData, setExploreMapData] = useState<{
    nodes: any[];
    buildings: any;
    edges: any[];
  } | null>(null);

  // Posts / carousel
  const [facebookPosts, setFacebookPosts] = useState<any[]>([]);
  const [localPosts, setLocalPosts] = useState<any[]>([]);
  const fbPosts = useMemo(
    () => [...localPosts, ...facebookPosts],
    [localPosts, facebookPosts],
  );
  const categoryEventPosts = useMemo(() => {
    if (!eventCategory) return [];
    return fbPosts.filter((p) => (p.category || "posts") === eventCategory);
  }, [fbPosts, eventCategory]);
  const eventCategoryCounts = useMemo(() => {
    const counts = { events: 0, competitions: 0, posts: 0 };
    for (const p of fbPosts) {
      const key = (p.category || "posts") as keyof typeof counts;
      if (key in counts) counts[key] += 1;
      else counts.posts += 1;
    }
    return counts;
  }, [fbPosts]);
  const latestPosts = useMemo(() => {
    return [...fbPosts].sort((a, b) => {
      const ta = new Date(a.created_time || 0).getTime();
      const tb = new Date(b.created_time || 0).getTime();
      return tb - ta;
    });
  }, [fbPosts]);
  const [currentSlide, setCurrentSlide] = useState(0);
  const lastKnownUploadRef = useRef(0);

  const isThinking = agentState === "thinking";
  const agentReady =
    agentState === "listening" ||
    agentState === "thinking" ||
    agentState === "speaking" ||
    agentState === "idle" ||
    agentState === "pre-connect-buffering";
  const isAgentInitializing = isConnected && !agentReady;

  // Explore map without a route: category hub first; full map only with navData or Explore
  const [showExploreMap, setShowExploreMap] = useState(false);

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
    [],
  );

  const sendEventFocus = useCallback(
    (event: any) => {
      const text =
        event.message || `Tell me about ${event.title || "this event"}`;
      window.dispatchEvent(
        new CustomEvent("nlu:inject_transcript", { detail: { text } }),
      );
    },
    [],
  );

  useEffect(() => {
    if (isConnected && pendingEventRef.current) {
      const ev = pendingEventRef.current;
      pendingEventRef.current = null;
      setTimeout(() => sendEventFocus(ev), 2500);
    }
  }, [isConnected, sendEventFocus]);

  // Navigation from NLU / LiveKit → open Maps overlay
  // Also handles: event poster routing and map→chat fallback routing
  useEffect(() => {
    if (!lastAction) return;
    if (lastAction === processedActionRef.current) return;

    // ── 1. Navigate action → open map ───────────────────────────────────
    if (lastAction.action === "navigate" && lastAction.destination) {
      processedActionRef.current = lastAction;
      setNavData({
        ...lastAction,
        path: lastAction.path ?? lastAction.path_coords,
        path_coords: lastAction.path_coords ?? lastAction.path,
        path_ids: lastAction.path_ids ?? [],
      });
      setMode("maps");
      setShowExploreMap(false);
      return;
    }

    // ── 2. Event poster action → switch to event detail view ─────────────
    const POSTER_ACTIONS = ["show_event_poster", "show_competition_poster", "show_campus_post"];
    if (POSTER_ACTIONS.includes(lastAction.action) && lastAction.target) {
      processedActionRef.current = lastAction;
      const targetFilename = lastAction.target as string;
      // Find the post whose image URL ends with the target filename
      const matched = fbPosts.find((p: any) => {
        const url: string = p.full_picture || p.image || p.filename || "";
        return url.includes(targetFilename);
      });
      if (matched) {
        handlePosterTapRef.current(matched);
      }
      return;
    }

    // ── 3. Non-navigate response while map is open → return to chat ──────
    if (mode === "maps" && lastAction.action !== "navigate") {
      processedActionRef.current = lastAction;
      // Close the map and go to talk mode so the transcript is visible
      setNavData(null);
      setShowExploreMap(false);
      setMode("talk");
      return;
    }
  }, [lastAction, fbPosts, mode]);

  // Pause timers when tab/screen hidden (CPU)
  useEffect(() => {
    const onVis = () => setPageVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  // Clock
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
    };
    updateTime();
    if (!pageVisible) return;
    const timer = setInterval(updateTime, 1000);
    return () => clearInterval(timer);
  }, [pageVisible]);

  // QR / upload IP (best-effort — route may be missing)
  useEffect(() => {
    async function fetchIp() {
      try {
        const res = await fetch("/api/network-ip");
        const data = await res.json();
        if (data.ip) {
          setQrUrl(`http://${data.ip}:3000/upload-portal`);
          return;
        }
      } catch {
        /* fall through */
      }
      if (typeof window !== "undefined") {
        setQrUrl(`http://${window.location.hostname}:3000/upload-portal`);
      }
    }
    fetchIp();
  }, []);

  // Local poster poll
  useEffect(() => {
    if (!pageVisible) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch("/api/upload-status");
        const data = await res.json();
        if (lastKnownUploadRef.current === 0) {
          lastKnownUploadRef.current = data.lastUpload;
        } else if (data.lastUpload > lastKnownUploadRef.current) {
          lastKnownUploadRef.current = data.lastUpload;
          setIsUploadModalOpen(false);
          setCurrentSlide(0);
        }
        if (data.allFiles) {
          setLocalPosts(
            data.allFiles.map((file: any) => {
              const categoryMap: Record<string, string> = {
                events: "Featured Campus Event",
                competitions: "Upcoming Competition",
                posts: "Campus Announcement",
              };
              const defaultTitle =
                categoryMap[file.category] || "Campus Highlight";
              // Use AI-extracted title if available, otherwise derive from filename.
              // e.g. "1780921427234_fit24_semester_end.jpg" → "Fit24 Semester End"
              // e.g. "1784340506849_34510.jpg" → falls back to category label
              let title = (file.extracted?.title || "").trim();
              if (!title) {
                const stem = (file.name as string).replace(/\.[^.]+$/, "");
                const parts = stem.split("_");
                const readable = parts.filter((p: string) => !/^\d+$/.test(p));
                const derived = readable.join(" ").replace(/-/g, " ").trim();
                title = derived && /[a-zA-Z]/.test(derived)
                  ? derived.replace(/\b\w/g, (c: string) => c.toUpperCase())
                  : defaultTitle;
              }
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
                name: file.name,
              };
            }),
          );
        }
      } catch {
        /* ignore */
      }
    }, 8000);
    return () => clearInterval(interval);
  }, [pageVisible]);

  // Facebook posts (optional API)
  useEffect(() => {
    const fetchPosts = async () => {
      try {
        const response = await fetch("/api/facebook");
        const data = await response.json();
        if (Array.isArray(data) && data.length > 0) setFacebookPosts(data);
      } catch {
        /* ignore */
      }
    };
    fetchPosts();
    if (!pageVisible) return;
    const interval = setInterval(fetchPosts, 30 * 60 * 1000);
    return () => clearInterval(interval);
  }, [pageVisible]);

  // Carousel — idle/events only, pause when hidden
  useEffect(() => {
    if (!pageVisible) return;
    if (mode !== "idle" && mode !== "events") return;
    if (focusedEvent) return;
    if (fbPosts.length <= 1) return;
    const interval = setInterval(() => {
      setCurrentSlide((prev) => (prev + 1) % fbPosts.length);
    }, 8000);
    return () => clearInterval(interval);
  }, [fbPosts.length, mode, focusedEvent, pageVisible]);

  // Lazy-load map graph + locations the first time Maps is opened
  const ensureMapsData = useCallback(async () => {
    if (mapsReady) return;
    try {
      const locRes = await fetch("/api/locations");
      const locData = await locRes.json();
      if (locData.locations) setAllLocations(locData.locations);

      const floorsFromLocations = Array.from(
        new Set((locData.locations || []).map((l: any) => l.floor).filter(Boolean)),
      ) as string[];
      const floors =
        floorsFromLocations.length > 0 ? floorsFromLocations : ["floor_1"];

      const mapResponses = await Promise.all(
        floors.map((floor) =>
          fetch(`/api/map?floor=${encodeURIComponent(floor)}`).then((r) =>
            r.json(),
          ),
        ),
      );

      const buildings: Record<string, any> = {};
      const edges: any[] = [];
      const nodes: any[] = [];

      mapResponses.forEach((data: any, idx) => {
        const floor = floors[idx] as string;
        const rawBuildings = data.buildings || {};
        const rawEdges = data.edges || [];
        const rawNodes = data.nodes || [];

        Object.entries(rawBuildings).forEach(([bId, b]: [string, any]) => {
          const scopedId = `${floor}::${bId}`;
          buildings[scopedId] = { ...b, floor };
        });

        rawNodes.forEach((n: any) => {
          const scopedBuildingId = `${floor}::${n.building}`;
          const b = buildings[scopedBuildingId] || { position: [0, 0, 0] };
          nodes.push({
            ...n,
            id: n.id || `${floor}::${n.label || "node"}`,
            building: scopedBuildingId,
            floor,
            world: [b.position[0] + n.x, 0, b.position[2] + n.z],
          });
        });

        edges.push(...rawEdges);
      });

      setExploreMapData({ nodes, buildings, edges });
      setMapsReady(true);
    } catch (e) {
      console.error("Failed to load map data:", e);
    }
  }, [mapsReady]);

  useEffect(() => {
    if (mode === "maps") {
      void ensureMapsData();
    }
  }, [mode, ensureMapsData]);

  // Auto-disconnect after 5 minutes of inactivity while talking
  const wasConnectedRef = useRef(isConnected);
  useEffect(() => {
    if (!isConnected && !isConnecting) {
      if (wasConnectedRef.current) {
        setFocusedEvent(null);
        pendingEventRef.current = null;
        setIsConnecting(false);
        if (mode === "talk") setMode("idle");
      }
      wasConnectedRef.current = false;
      return;
    }
    wasConnectedRef.current = true;
    const timeoutId = setTimeout(
      () => {
        end();
      },
      5 * 60 * 1000,
    );
    return () => clearTimeout(timeoutId);
  }, [isConnected, end, messages, transcriptions, mode]);

  useEffect(() => {
    if (isConnected && !isAgentInitializing && isConnecting) {
      setIsConnecting(false);
    }
  }, [isConnected, isAgentInitializing, isConnecting]);

  // Single live caption for Talk mode (no chat history)
  const talkCaption = useMemo(() => {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg) {
      const text =
        lastMsg.content || lastMsg.message || lastMsg.text || "";
      if (text.trim()) {
        const isUser =
          lastMsg.role === "user" ||
          lastMsg.from?.isLocal === true ||
          lastMsg.participantIdentity === "user";
        return { text: text.trim(), isUser };
      }
    }
    const lastTx = transcriptions[transcriptions.length - 1];
    if (lastTx?.text) {
      return { text: String(lastTx.text).trim(), isUser: true };
    }
    if (isThinking) return { text: "Thinking…", isUser: false };
    if (isConnected) return { text: "Listening…", isUser: false };
    return { text: "Tap the mic to talk", isUser: false };
  }, [
    messages,
    transcriptions,
    isThinking,
    isAgentInitializing,
    isConnected,
  ]);

  const handleMicClick = useCallback(async () => {
    if (isConnected) {
      end();
      await new Promise((r) => setTimeout(r, 400));
      return;
    }
    setIsConnecting(true);
    setMode("talk");
    try {
      const timeoutPromise = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("Connection timeout")), 90_000),
      );
      await Promise.race([startSession(), timeoutPromise]);
    } catch (e: any) {
      console.error("Agent connection failed:", e);
      const msg = e?.message || String(e);
      if (e?.name === "NotAllowedError" || msg.includes("getUserMedia")) {
        alert(
          "Microphone access blocked! Use http://localhost (not an IP) or HTTPS.",
        );
      }
      setIsConnecting(false);
    }
  }, [isConnected, startSession, end]);

  const openEvents = useCallback(() => {
    setMode("events");
    setFocusedEvent(null);
    setEventCategory(null);
    setNavData(null);
    setShowExploreMap(false);
  }, []);

  const openMaps = useCallback(() => {
    setMode("maps");
    setFocusedEvent(null);
    setNavData(null);
    setShowExploreMap(false);
    setLocationsModalCategory(null);
    void ensureMapsData();
  }, [ensureMapsData]);

  const openTalk = useCallback(() => {
    setMode("talk");
    setFocusedEvent(null);
    setShowExploreMap(false);
  }, []);

  const goIdle = useCallback(() => {
    setMode("idle");
    setFocusedEvent(null);
    setEventCategory(null);
    setNavData(null);
    setShowExploreMap(false);
    setLocationsModalCategory(null);
    if (isConnected) end();
  }, [isConnected, end]);

  /** Idle/events poster tap — detail only; voice only if Talk already live */
  const handlePosterTap = useCallback(
    (post: any) => {
      setFocusedEvent(post);
      setMode("events");
      const cat = post.category;
      if (cat === "events" || cat === "competitions" || cat === "posts") {
        setEventCategory(cat);
      }
      if (isConnected) {
        sendEventFocus(post);
      }
    },
    [isConnected, sendEventFocus],
  );
  // Keep the ref in sync so the lastAction effect always has the latest version
  handlePosterTapRef.current = handlePosterTap;

  const handleCategoryClick = (category: string, filterKeyword: string) => {
    setLocationsModalCategory(category);
    setShowExploreMap(false);
    if (filterKeyword === "office") {
      setFilteredLocations(
        allLocations.filter((loc) => {
          const l = loc.label.toLowerCase();
          return (
            !l.includes("lecture") &&
            !l.includes("lab") &&
            !l.includes("stair")
          );
        }),
      );
    } else {
      setFilteredLocations(
        allLocations.filter((loc) =>
          loc.label.toLowerCase().includes(filterKeyword.toLowerCase()),
        ),
      );
    }
  };

  const handleNavigateToLocation = async (destination: string) => {
    setLocationsModalCategory(null);
    setShowExploreMap(false);
    try {
      const res = await fetch(
        `/api/navigate?destination=${encodeURIComponent(destination)}`,
      );
      const data = await res.json();
      if (data && data.path_coords) {
        setNavData({
          type: "navigation",
          destination: data.destination,
          floor: data.floor,
          path: data.path_coords,
          path_coords: data.path_coords,
          path_ids: data.path_ids || [],
          directions: data.directions || "",
          nodes: data.nodes || [],
          buildings: data.buildings || {},
        });
        setMode("maps");
        if (isConnected) {
          sendEventFocus({
            title: destination,
            message: `Please give me directions to ${destination}`,
            category: "navigation",
          });
        }
      } else {
        console.error("Navigate API error:", data);
      }
    } catch (e) {
      console.error("Failed to fetch navigation:", e);
    }
  };

  const closeNav = () => {
    setNavData(null);
    setShowExploreMap(false);
    setMode(isConnected ? "talk" : "idle");
  };

  // Swipe on banner
  const touchStartRef = useRef<number | null>(null);
  const touchEndRef = useRef<number | null>(null);
  const onTouchStart = (e: React.TouchEvent) => {
    touchEndRef.current = null;
    touchStartRef.current = e.targetTouches[0].clientX;
  };
  const onTouchMove = (e: React.TouchEvent) => {
    touchEndRef.current = e.targetTouches[0].clientX;
  };
  const onTouchEnd = () => {
    if (touchStartRef.current == null || touchEndRef.current == null) return;
    const distance = touchStartRef.current - touchEndRef.current;
    if (distance > 50 && fbPosts.length > 0) {
      setCurrentSlide((prev) => (prev + 1) % fbPosts.length);
    }
    if (distance < -50 && fbPosts.length > 0) {
      setCurrentSlide(
        (prev) => (prev - 1 + fbPosts.length) % fbPosts.length,
      );
    }
  };

  const showMapCanvas = Boolean(navData) || showExploreMap;
  const mountMap = mode === "maps" && showMapCanvas;
  const hasTranscript = isConnected || isThinking || talkCaption.text !== "Tap the mic to talk";

  return (
    <div
      className="kiosk-mode relative text-[var(--kiosk-text)] w-full h-screen overflow-hidden flex flex-col select-none bg-[var(--kiosk-bg)]"
      style={{
        touchAction: "manipulation",
        fontFamily: "var(--font-jakarta), sans-serif",
      }}
    >
      <div className={`relative z-10 w-full h-full flex flex-col ${PAGE_PAD}`}>
        {/* Thin top bar */}
        <header className="flex-shrink-0 w-full flex justify-between items-center h-[52px] z-20">
          <div className="text-[22px] font-black tracking-tight text-[var(--kiosk-text)]">
            NEma
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[16px] font-semibold tabular-nums text-[var(--kiosk-muted)] mr-1">
              {time || "—"}
            </span>
            <PopButton
              onClick={() => setIsUploadModalOpen(true)}
              className="min-h-[40px] px-3.5 rounded-full border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] flex items-center gap-2 text-[14px] font-bold"
              aria-label="Upload poster"
            >
              <UploadCloud className="w-4 h-4" />
              Upload
            </PopButton>
            <PopButton
              onClick={() => setIsSettingsOpen(true)}
              className="p-2.5 rounded-full border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)]"
              aria-label="Settings"
            >
              <Settings className="w-5 h-5" />
            </PopButton>
          </div>
        </header>

        {/* Main stage — full stretch; floating dock overlays the bottom */}
        <main className="flex-1 min-h-0 flex flex-col relative overflow-hidden">
          {/* NAV / EXPLORE MAP overlay */}
          {mode === "maps" && mountMap ? (
            <div className={`flex-1 min-h-0 ${PANEL} overflow-hidden relative bg-[var(--kiosk-surface-muted)]`}>
              {navData ? (
                <>
                  {/* Floating Transcript for Navigation Map Mode */}
                  {hasTranscript && (
                    <div className="absolute top-4 right-20 flex flex-col items-end z-40 pointer-events-none">
                      <div className="bg-black/60 backdrop-blur-md text-white px-5 py-4 rounded-3xl text-left max-w-sm shadow-xl border border-white/10">
                        <div className="font-semibold text-[24px] leading-relaxed">
                          {talkCaption.text.includes("\n") || talkCaption.text.includes(", then ") ? (
                            <ul className="list-disc pl-5 space-y-1">
                              {talkCaption.text.split(/(?:\.\n|, then )/).map((step: string, i: number) => {
                                const clean = step.trim().replace(/\.$/, "");
                                return clean ? <li key={i}>{clean}</li> : null;
                              })}
                            </ul>
                          ) : (
                            talkCaption.text
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  <PopButton
                    onClick={closeNav}
                    aria-label="Close"
                    className={`absolute top-4 right-4 z-30 ${ICON_BTN} bg-[var(--kiosk-surface)] shadow-sm`}
                  >
                    <span className="material-symbols-outlined text-[36px]">
                      close
                    </span>
                  </PopButton>
                  <Suspense
                    fallback={
                      <div className="flex h-full items-center justify-center text-[var(--kiosk-muted)]">
                        Loading map…
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
                      onClose={closeNav}
                    />
                  </Suspense>
                </>
              ) : showExploreMap && exploreMapData ? (
                <>
                  {/* Floating Transcript for Explore Map Mode */}
                  {hasTranscript && (
                    <div className="absolute top-4 right-20 flex flex-col items-end z-40 pointer-events-none">
                      <div className="bg-black/60 backdrop-blur-md text-white px-5 py-4 rounded-3xl text-left max-w-sm shadow-xl border border-white/10">
                        <div className="font-semibold text-[24px] leading-relaxed">
                          {talkCaption.text.includes("\n") || talkCaption.text.includes(", then ") ? (
                            <ul className="list-disc pl-5 space-y-1">
                              {talkCaption.text.split(/(?:\.\n|, then )/).map((step: string, i: number) => {
                                const clean = step.trim().replace(/\.$/, "");
                                return clean ? <li key={i}>{clean}</li> : null;
                              })}
                            </ul>
                          ) : (
                            talkCaption.text
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  <PopButton
                    onClick={() => setShowExploreMap(false)}
                    aria-label="Close map"
                    className={`absolute top-4 right-4 z-30 ${ICON_BTN} bg-[var(--kiosk-surface)] shadow-sm`}
                  >
                    <span className="material-symbols-outlined text-[36px]">
                      close
                    </span>
                  </PopButton>
                  <Suspense
                    fallback={
                      <div className="flex h-full items-center justify-center text-[var(--kiosk-muted)]">
                        Loading map…
                      </div>
                    }
                  >
                    <NavigationMap
                      key="explore-map"
                      path={[]}
                      path_ids={[]}
                      destination=""
                      nodes={exploreMapData.nodes}
                      buildings={exploreMapData.buildings}
                      edges={exploreMapData.edges}
                      isStandalone={true}
                      onNodeClick={handleNavigateToLocation}
                    />
                  </Suspense>
                </>
              ) : null}
            </div>
          ) : mode === "maps" ? (
            /* Maps hub — category buttons only (no WebGL until route/explore) */
            <div className={`flex-1 min-h-0 ${PANEL} p-6 flex flex-col`}>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-[40px] font-bold text-[var(--kiosk-text)]">
                  Where to?
                </h2>
                <div className="flex items-center gap-3">
                  {/* Compact Explore Map Action Button */}
                  <PopButton
                    onClick={() => {
                      setNavData(null);
                      void ensureMapsData().then(() => setShowExploreMap(true));
                    }}
                    className="flex items-center gap-2 px-5 py-2.5 rounded-full bg-blue-600 hover:bg-blue-700 text-white text-[24px] font-bold transition-colors shadow-md border border-transparent"
                  >
                    <span className="material-symbols-outlined text-[36px]">
                      map
                    </span>
                    Explore Map
                  </PopButton>

                  <PopButton
                    onClick={goIdle}
                    aria-label="Close"
                    className={ICON_BTN}
                  >
                    <span className="material-symbols-outlined text-[22px]">
                      close
                    </span>
                  </PopButton>
                </div>
              </div>
              <p className="text-[16px] text-[var(--kiosk-muted)] mb-6">
                Pick a category, then choose a room.
              </p>
              <div className="flex flex-col gap-6 mt-auto pb-12 w-full">
                {/* Horizontal Category Carousel */}
                <div className="flex justify-center gap-4 overflow-x-auto snap-x snap-mandatory pb-4 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden w-full">
                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => handleCategoryClick("Lecture Halls", "lecture")}
                  >
                    <span className="material-symbols-outlined text-[#EA580C]" style={{ fontSize: "66px" }}>
                      school
                    </span>
                    <span className="text-center leading-tight">Lecture Halls</span>
                  </PopButton>

                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => handleCategoryClick("Laboratory", "lab")}
                  >
                    <span className="material-symbols-outlined text-[#8B5CF6]" style={{ fontSize: "66px" }}>
                      science
                    </span>
                    <span className="text-center leading-tight">Laboratory</span>
                  </PopButton>

                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => handleCategoryClick("Offices & More", "office")}
                  >
                    <span className="material-symbols-outlined text-[#14B8A6]" style={{ fontSize: "66px" }}>
                      apartment
                    </span>
                    <span className="text-center leading-tight">Offices &amp; More</span>
                  </PopButton>
                </div>
              </div>
            </div>
          ) : mode === "talk" ? (
            <div className={`flex-1 min-h-0 ${PANEL} relative overflow-hidden bg-[var(--kiosk-surface-muted)]`}>
              {/* Poster/Image on the Left */}
              {focusedEvent && (
                <div className="absolute left-0 top-0 bottom-0 w-1/2 min-w-0 flex items-center justify-center bg-black/20 overflow-hidden">
                  <img
                    src={focusedEvent.full_picture}
                    alt={focusedEvent.message}
                    className="w-full h-full object-cover"
                  />
                </div>
              )}

              {/* Transcript area (centered when no poster, right-aligned split-screen when poster is active) */}
              <div className={
                focusedEvent
                  ? "absolute right-0 top-0 bottom-0 w-1/2 min-w-0 flex flex-col items-center justify-center gap-6 px-8 py-6"
                  : "w-full h-full flex flex-col items-center justify-center gap-6 px-6 relative"
              }>
                <PopButton
                  onClick={goIdle}
                  aria-label="Close"
                  className={`absolute top-4 right-4 ${ICON_BTN}`}
                >
                  <span className="material-symbols-outlined text-[36px]">
                    close
                  </span>
                </PopButton>

                <div className="flex-1 flex flex-col items-center justify-center gap-6 w-full">
                  <p className={`text-[42px] font-semibold text-center text-[var(--kiosk-text)] min-h-[3rem] px-4 ${
                    focusedEvent ? "max-w-sm" : "max-w-lg"
                  }`}>
                    {talkCaption.isUser ? (
                      <span className="opacity-70">You: </span>
                    ) : null}
                    {talkCaption.text}
                  </p>

                  {/* Dynamic Buttons injected via NLU Action payload */}
                  {lastAction?.suggested_buttons && lastAction.suggested_buttons.length > 0 && (
                    <div className="flex flex-wrap items-center justify-center gap-3 mt-2">
                      {(lastAction.suggested_buttons as Array<string | { label: string; filename: string; category: string }>).map((btn, idx: number) => {
                        // Buttons can be plain strings (navigation/smalltalk) or
                        // event descriptor objects { label, filename, category }.
                        const isEventBtn = typeof btn === "object" && btn !== null;
                        const btnLabel = isEventBtn ? btn.label : btn;
                        const btnFilename = isEventBtn ? btn.filename : null;

                        // For event buttons: match by filename (exact), then fall
                        // back to message match for legacy string buttons.
                        const matchingPost = btnFilename
                          ? fbPosts.find((p) => (p.name as string) === btnFilename)
                          : fbPosts.find(
                            (p) =>
                              (p.message || "").toLowerCase().trim() ===
                              btnLabel.toLowerCase().trim(),
                          );

                        const handleButtonClick = () => {
                          if (matchingPost) {
                            handlePosterTap(matchingPost);
                          } else {
                            sendSimulatedVoice(btnLabel);
                          }
                        };
                        return (
                          <PopButton
                            key={idx}
                            onClick={handleButtonClick}
                            className="px-6 py-4 rounded-full bg-white/40 dark:bg-black/40 border border-[var(--kiosk-border)] shadow-sm backdrop-blur-md text-[19px] font-medium text-[var(--kiosk-text)] hover:bg-white/60 dark:hover:bg-white/10 transition-colors active:scale-95 min-h-[56px]"
                          >
                            {btnLabel}
                          </PopButton>
                        );
                      })}
                    </div>
                  )}

                  {!isConnected && (
                    <p className="text-[15px] text-[var(--kiosk-muted)] mt-2">
                      Ask about events or directions
                    </p>
                  )}
                </div>
              </div>
            </div>
          ) : mode === "events" && focusedEvent ? (
            /* Focused poster with category + meta */
            <div className="flex-1 min-h-0 rounded-[28px] overflow-hidden bg-neutral-900 flex flex-col relative">
              <PopButton
                onClick={() => setFocusedEvent(null)}
                aria-label="Close"
                className="absolute top-4 right-4 z-10 w-20 h-20 bg-black/50 text-white rounded-full flex items-center justify-center"
              >
                <span className="material-symbols-outlined text-[36px]">
                  close
                </span>
              </PopButton>
              <div className={`flex-1 min-h-0 transition-all duration-300 ${hasTranscript ? "w-1/2 pl-8" : "w-full"}`}>
                <img
                  src={focusedEvent.full_picture}
                  alt={focusedEvent.message}
                  className="w-full h-full object-contain"
                />
              </div>
              <div className={`shrink-0 p-5 bg-black/80 text-white space-y-2 transition-all duration-300 ${hasTranscript ? "w-1/2 border-r border-white/10" : "w-full"}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`text-[11px] font-bold uppercase tracking-wider px-2.5 py-1 rounded-full ${eventCategoryMeta(focusedEvent.category).chip
                      }`}
                  >
                    {eventCategoryMeta(focusedEvent.category).label}
                  </span>
                  {focusedEvent.extracted_date && (
                    <span className="text-[12px] font-semibold opacity-80">
                      {focusedEvent.extracted_date}
                      {focusedEvent.extracted_time
                        ? ` · ${focusedEvent.extracted_time}`
                        : ""}
                    </span>
                  )}
                  {focusedEvent.extracted_location && (
                    <span className="text-[12px] opacity-70">
                      {focusedEvent.extracted_location}
                    </span>
                  )}
                </div>
                <p className="font-semibold text-[24px] leading-tight">
                  {focusedEvent.message}
                </p>
                {focusedEvent.description && (
                  <p className="text-[14px] opacity-80 line-clamp-3">
                    {focusedEvent.description}
                  </p>
                )}
              </div>

              {/* Floating Transcript for Event/Poster Mode (matching Map Mode) */}
              {hasTranscript && (
                <div className="absolute top-4 right-20 flex flex-col items-end z-40 pointer-events-none">
                  <div className="bg-black/60 backdrop-blur-md text-white px-5 py-4 rounded-3xl text-left max-w-sm shadow-xl border border-white/10">
                    <div className="font-semibold text-[24px] leading-relaxed">
                      {talkCaption.text.includes("\n") || talkCaption.text.includes(", then ") ? (
                        <ul className="list-disc pl-5 space-y-1">
                          {talkCaption.text.split(/(?:\.\n|, then )/).map((step: string, i: number) => {
                            const clean = step.trim().replace(/\.$/, "");
                            return clean ? <li key={i}>{clean}</li> : null;
                          })}
                        </ul>
                      ) : (
                        talkCaption.text
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          ) : mode === "events" && eventCategory ? (
            /* Category list — picked Competitions / Events / Announcements */
            <div className={`flex-1 min-h-0 ${PANEL} flex flex-col`}>
              <div className="shrink-0 flex items-center justify-between px-5 pt-5 pb-3">
                <div className="flex items-center gap-3 min-w-0">
                  <span
                    className={`w-3 h-3 rounded-full shrink-0 ${eventCategoryMeta(eventCategory).accent
                      }`}
                  />
                  <h2 className="text-[26px] font-bold text-[var(--kiosk-text)] truncate">
                    {eventCategory === "competitions"
                      ? "Competitions"
                      : eventCategory === "events"
                        ? "Campus Events"
                        : "Announcements"}
                  </h2>
                </div>
                <PopButton
                  onClick={() => setEventCategory(null)}
                  aria-label="Close"
                  className={ICON_BTN}
                >
                  <span className="material-symbols-outlined text-[36px]">
                    close
                  </span>
                </PopButton>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto px-4 pb-36 [-webkit-overflow-scrolling:touch] scroll-smooth [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]">
                {categoryEventPosts.length === 0 ? (
                  <div className="h-full min-h-[200px] flex flex-col items-center justify-center gap-2 opacity-50">
                    <span className="material-symbols-outlined text-4xl">
                      newspaper
                    </span>
                    <p className="text-[15px] text-center">
                      Nothing here yet. Upload a poster to add one.
                    </p>
                  </div>
                ) : (
                  <div className="grid grid-cols-2 gap-3">
                    {categoryEventPosts.map((post) => {
                      const meta = eventCategoryMeta(post.category);
                      return (
                        <PopButton
                          key={post.id}
                          type="button"
                          onClick={() => handlePosterTap(post)}
                          className="text-left rounded-2xl overflow-hidden border border-[var(--kiosk-border)] bg-[var(--kiosk-surface-muted)] flex flex-col min-h-[220px] h-full"
                          >
                          <div className="relative w-full aspect-[4/3] shrink-0 overflow-hidden bg-[var(--kiosk-border)]">
                            <img
                              src={post.full_picture}
                              alt=""
                              className="w-full h-full object-cover"
                            />

                          </div>
                          <div className="flex-1 p-3 min-w-0 flex flex-col">
                                  {post.extracted_date && (
                              <p className="text-[14px] font-semibold text-[var(--kiosk-muted)] mb-1 line-clamp-1">
                                {post.extracted_date}
                                {post.extracted_location
                                  ? ` · ${post.extracted_location}`
                                  : ""}
                              </p>
                            )}
                            <p className="text-[18px] font-semibold text-[var(--kiosk-text)] leading-tight line-clamp-2">
                                  {post.message}
                                </p>
                                {post.description && (
                              <p className="text-[14px] text-[var(--kiosk-muted)] mt-1 line-clamp-2">
                                    {post.description}
                                  </p>
                                )}
                              </div>
                        </PopButton>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : mode === "events" ? (
            /* Events hub — same pattern as Maps: pick a category first */
            <div className={`flex-1 min-h-0 ${PANEL} p-6 flex flex-col`}>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-[36px] font-bold text-[var(--kiosk-text)]">
                  What&apos;s on?
                </h2>
                <PopButton
                  onClick={goIdle}
                  aria-label="Close"
                  className={ICON_BTN}
                >
                  <span className="material-symbols-outlined text-[36px]">
                    close
                  </span>
                </PopButton>
              </div>
              <p className="text-[16px] text-[var(--kiosk-muted)] mb-6">
                Pick a category, then open a poster.
              </p>
              <div className="flex flex-col gap-6 mt-auto pb-12 w-full">
                {/* Horizontal Category Carousel */}
                <div className="flex justify-center gap-4 overflow-x-auto snap-x snap-mandatory pb-4 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden w-full">
                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => setEventCategory("competitions")}
                  >
                    <span className="material-symbols-outlined text-[#F97316]" style={{ fontSize: "66px" }}>
                      emoji_events
                    </span>
                    <span className="text-center leading-tight">Competitions</span>
                  </PopButton>

                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => setEventCategory("events")}
                  >
                    <span className="material-symbols-outlined text-[#7C3AED]" style={{ fontSize: "66px" }}>
                      celebration
                    </span>
                    <span className="text-center leading-tight">Campus Events</span>
                  </PopButton>

                  <PopButton
                    className="flex-1 max-w-[400px] aspect-square rounded-[24px] text-[36px] font-bold flex flex-col items-center justify-center gap-2 border border-[var(--kiosk-border)] bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] snap-center shrink-0"
                    onClick={() => setEventCategory("posts")}
                  >
                    <span className="material-symbols-outlined text-[#14B8A6]" style={{ fontSize: "66px" }}>
                      campaign
                    </span>
                    <span className="text-center leading-tight">Announcements</span>
                  </PopButton>
                </div>
              </div>
            </div>
          ) : (
            /* Idle — two-zone: featured banner + always-visible "What's New" rail */
            <div className="flex-1 min-h-0 flex gap-3">
              {/* Featured banner — image-first; caption/dots clear of floating island */}
              <div
                className="flex-1 min-w-0 rounded-[28px] overflow-hidden relative bg-neutral-800"
                onTouchStart={onTouchStart}
                onTouchMove={onTouchMove}
                onTouchEnd={onTouchEnd}
              >
                {fbPosts.length > 0 ? (
                  <>
                    {fbPosts.map((post, index) => (
                      <PopButton
                        key={post.id}
                        type="button"
                        className={`absolute inset-0 w-full h-full transition-opacity duration-700 ${index === currentSlide
                          ? "opacity-100 pointer-events-auto"
                          : "opacity-0 pointer-events-none"
                          }`}
                        onClick={() => handlePosterTap(post)}
                      >
                        <img
                          alt=""
                          className="absolute inset-0 w-full h-full object-cover"
                          src={post.full_picture}
                        />
                      </PopButton>
                    ))}
                    {/* Soft vignette for text legibility — no card/box */}
                    <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-black/50 via-transparent to-transparent" />

                    {/* Featured caption — open text, no frosted box */}
                    {(() => {
                      const post = fbPosts[currentSlide % fbPosts.length];
                      if (!post) return null;
                      const meta = eventCategoryMeta(post.category);
                      return (
                        <div className="pointer-events-none absolute top-5 left-5 right-28 z-10 text-white">
                          <div className="flex items-center gap-2 mb-1.5">
                            {post.category && (
                              <>
                                <span
                                  className={`inline-block w-2 h-2 rounded-full shrink-0 ${meta.accent}`}
                                />
                                <span className="text-[11px] font-bold uppercase tracking-[0.14em] text-white/85 drop-shadow">
                                  {meta.label}
                                </span>
                              </>
                            )}
                            {(post.extracted_date ||
                              post.extracted_location) && (
                                <span className="text-[11px] font-medium text-white/70 drop-shadow">
                                  ·{" "}
                                  {[post.extracted_date, post.extracted_location]
                                    .filter(Boolean)
                                    .join(" · ")}
                                </span>
                              )}
                          </div>
                          <p className="text-[24px] font-bold leading-snug line-clamp-2 drop-shadow-[0_2px_8px_rgba(0,0,0,0.65)]">
                            {post.message}
                          </p>
                        </div>
                      );
                    })()}

                    {/* Vertical slide rail — left edge, clear of island */}
                    {fbPosts.length > 1 && (
                      <div className="absolute left-3 top-1/2 -translate-y-1/2 z-10 flex flex-col gap-2 pointer-events-none">
                        {fbPosts.map((_, idx) => (
                          <div
                            key={idx}
                            className={`rounded-full transition-all ${idx === currentSlide
                              ? "w-2 h-6 bg-white"
                              : "w-2 h-2 bg-white/45"
                              }`}
                          />
                        ))}
                      </div>
                    )}
                  </>
                ) : (
                  <div className="absolute inset-0 flex flex-col items-center justify-center text-white/70 gap-3 px-8 text-center">
                    <span className="material-symbols-outlined text-5xl opacity-40">
                      campaign
                    </span>
                    <p className="text-[24px] font-semibold">
                      Welcome to the Faculty of IT
                    </p>
                    <p className="text-[15px] opacity-70">
                      Tap Maps for directions, or Talk to ask NEma
                    </p>
                  </div>
                )}
              </div>

              {/* What's New rail — newest first */}
              <div className={`w-[300px] shrink-0 ${PANEL} flex flex-col overflow-hidden`}>
                <div className="shrink-0 px-4 pt-4 pb-2 flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-[var(--kiosk-success)] animate-pulse" />
                  <h2 className="text-[18px] font-bold text-[var(--kiosk-text)]">
                    What&apos;s New
                  </h2>
                </div>
                <div className="flex-1 min-h-0 overflow-y-auto px-3 pb-36 space-y-2 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
                  {latestPosts.length === 0 ? (
                    <div className="h-full flex flex-col items-center justify-center gap-2 text-[var(--kiosk-muted)] py-8 text-center">
                      <span className="material-symbols-outlined text-4xl">
                        newspaper
                      </span>
                      <p className="text-[13px]">No posters yet</p>
                    </div>
                  ) : (
                    latestPosts.slice(0, 8).map((post) => {
                      const meta = eventCategoryMeta(post.category);
                      return (
                        <PopButton
                          key={post.id}
                          type="button"
                          onClick={() => handlePosterTap(post)}
                          className="w-full text-left rounded-2xl overflow-hidden border border-[var(--kiosk-border)] bg-[var(--kiosk-surface-muted)]"
                        >
                          <div className="flex">
                            <div className="relative w-[64px] shrink-0 overflow-hidden bg-[var(--kiosk-border)]">
                              <img
                                src={post.full_picture}
                                alt=""
                                className="w-full h-full object-cover min-h-[64px]"
                              />

                            </div>
                            <div className="flex-1 p-2.5 min-w-0">
                              <div className="flex items-center gap-1.5 mb-0.5">
                                <span
                                  className={`inline-block w-2 h-2 rounded-full shrink-0 ${meta.accent}`}
                                />
                                <span className="text-[10px] font-bold uppercase tracking-wider text-[var(--kiosk-muted)]">
                                  {meta.label}
                                </span>
                              </div>
                              <p className="text-[13px] font-semibold text-[var(--kiosk-text)] leading-tight line-clamp-2">
                                {post.message}
                              </p>
                              {post.extracted_date && (
                                <p className="text-[11px] text-[var(--kiosk-muted)] mt-0.5 line-clamp-1">
                                  {post.extracted_date}
                                </p>
                              )}
                            </div>
                          </div>
                        </PopButton>
                      );
                    })
                  )}
                </div>
              </div>
            </div>
          )}
        </main>

        {/* Floating island dock — overlays banner / What's New */}
        <nav className="pointer-events-none absolute left-0 right-0 bottom-4 z-30 flex justify-center">
          <div className="pointer-events-auto flex items-center gap-1.5 rounded-[32px] bg-white/55 dark:bg-black/45 border border-white/40 dark:border-white/15 shadow-[0_12px_40px_rgba(0,0,0,0.22)] backdrop-blur-2xl supports-[backdrop-filter]:bg-white/40 dark:supports-[backdrop-filter]:bg-black/35 px-2 py-1.5">
            <PopButton
              type="button"
              className={`${DOCK_BTN} ${mode === "events" ? DOCK_BTN_ACTIVE : DOCK_BTN_IDLE}`}
              onClick={openEvents}
            >
              <span className="material-symbols-outlined text-[24px]">
                campaign
              </span>
              Events
            </PopButton>

            <div className="relative mx-1 flex items-center justify-center w-[88px] h-[72px]">
              <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-10">
                <GeminiMorphButton
                  size={80}
                  volume={agentState === "listening" ? maxVolume : 0}
                  isAnimating={
                    isConnecting ||
                    isAgentInitializing ||
                    isConnected ||
                    isThinking
                  }
                  isConnected={isConnected}
                  onClick={() => {
                    if (mode !== "talk") {
                      openTalk();
                      if (!isConnected) void handleMicClick();
                    } else {
                      void handleMicClick();
                    }
                  }}
                />
              </div>
            </div>

            <PopButton
              type="button"
              className={`${DOCK_BTN} ${mode === "maps" ? DOCK_BTN_ACTIVE : DOCK_BTN_IDLE}`}
              onClick={openMaps}
            >
              <span className="material-symbols-outlined text-[24px]">map</span>
              Maps
            </PopButton>
          </div>
        </nav>
      </div>

      {/* Locations category sheet */}
      <AnimatePresence>
        {locationsModalCategory && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/50 flex items-end justify-center"
            onClick={() => setLocationsModalCategory(null)}
          >
            <motion.div
              initial={{ y: "100%" }}
              animate={{ y: 0 }}
              exit={{ y: "100%" }}
              transition={{ type: "spring", stiffness: 320, damping: 32 }}
              className="bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] w-full max-w-2xl rounded-t-3xl max-h-[70vh] overflow-hidden flex flex-col"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="shrink-0 flex items-center justify-between p-6 pb-4">
                <h3 className="text-[22px] font-bold">
                  {locationsModalCategory}
                </h3>
                <PopButton
                  onClick={() => setLocationsModalCategory(null)}
                  className="p-2 rounded-full bg-black/5 dark:bg-white/10"
                >
                  <X className="w-5 h-5" />
                </PopButton>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto px-6 pb-6 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
                {filteredLocations.length === 0 ? (
                  <p className="text-center py-8 opacity-60">
                    No rooms found in this category.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {filteredLocations.map((loc) => (
                      <PopButton
                        key={`${loc.floor}-${loc.id}`}
                        onClick={() => handleNavigateToLocation(loc.label)}
                        className="min-h-[56px] bg-[var(--kiosk-surface-muted)] border border-[var(--kiosk-border)] rounded-2xl w-full text-[16px] flex items-center justify-between px-5 font-semibold"
                      >
                        <span className="truncate text-left">{loc.label}</span>
                        <span className="text-[12px] opacity-50 flex-shrink-0 ml-3">
                          {(loc.floor as string).replace("floor_", "Floor ")}
                        </span>
                      </PopButton>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Settings sheet */}
      <AnimatePresence>
        {isSettingsOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/50 flex items-end justify-center"
            onClick={() => setIsSettingsOpen(false)}
          >
            <motion.div
              initial={{ y: "100%" }}
              animate={{ y: 0 }}
              exit={{ y: "100%" }}
              transition={{ type: "spring", stiffness: 320, damping: 32 }}
              className="bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] w-full max-w-md rounded-t-3xl p-6 space-y-4"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between">
                <h3 className="text-[24px] font-bold">Settings</h3>
                <PopButton
                  onClick={() => setIsSettingsOpen(false)}
                  className="p-2 rounded-full bg-black/5 dark:bg-white/10"
                >
                  <X className="w-5 h-5" />
                </PopButton>
              </div>
              <p className="text-[12px] font-bold uppercase tracking-wide text-amber-600">
                NLU mode
              </p>
              
              <div className="flex items-center justify-between py-2 border-b border-[var(--kiosk-border)]">
                <div className="flex flex-col">
                  <span className="font-semibold">Auto-Greet</span>
                  <span className="text-[12px] text-[var(--kiosk-muted)]">Greet people when they approach</span>
                </div>
                <PopButton
                  onClick={toggleFaceGreeting}
                  className={`w-12 h-6 rounded-full transition-colors relative ${
                    faceGreetingEnabled ? "bg-[#10B981]" : "bg-[var(--kiosk-border)]"
                  }`}
                  aria-label="Toggle Auto Greet"
                >
                  <span 
                    className={`absolute top-1 bottom-1 w-4 bg-white rounded-full transition-all shadow-sm ${
                      faceGreetingEnabled ? "left-7" : "left-1"
                    }`}
                  />
                </PopButton>
              </div>
              <div className="flex items-center justify-between py-2">
                <span className="font-semibold">Theme</span>
                <ThemeToggle />
              </div>
              <div className="py-2 space-y-3">
                <span className="font-semibold">Eye color</span>
                <div className="flex items-center gap-2.5 overflow-x-auto [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
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
                    { name: "Cyan", eye: "cyan", ui: "", swatch: "bg-cyan-400" },
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
                      className={`w-10 h-10 rounded-full shrink-0 shadow-sm ${c.swatch} ${c.name === "White"
                        ? "border-2 border-[var(--kiosk-border)] ring-1 ring-[var(--kiosk-muted)]"
                        : "border border-[var(--kiosk-border)]"
                        }`}
                      aria-label={`Change eye color to ${c.name}`}
                    />
                  ))}
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Upload QR */}
      {isUploadModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-[var(--kiosk-surface)] text-[var(--kiosk-text)] p-8 rounded-3xl max-w-md w-full relative mx-4">
            <PopButton
              onClick={() => setIsUploadModalOpen(false)}
              className="absolute top-4 right-4 p-2 rounded-full bg-[var(--kiosk-surface-muted)]"
            >
              <X className="w-5 h-5" />
            </PopButton>
            <div className="flex flex-col items-center text-center space-y-5">
              <UploadCloud className="w-8 h-8" />
              <h2 className="text-2xl font-bold">Upload a Poster</h2>
              <div className="bg-white p-4 rounded-2xl">
                <QRCodeSVG value={qrUrl || "http://localhost:3000/upload-portal"} size={180} />
              </div>
              <p className="text-sm opacity-60 break-all">{qrUrl}</p>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
