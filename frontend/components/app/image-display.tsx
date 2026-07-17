"use client";

import { AnimatePresence, motion } from "motion/react";
import { useSessionContext } from "@livekit/components-react";
import { cn } from "@/lib/utils";
import { useEffect, useState, Suspense } from "react";
import { DataPacket_Kind, RemoteParticipant } from "livekit-client";
import dynamic from "next/dynamic";
import LoadingOverlay from "@/components/ui/LoadingOverlay";

// Lazy load the 3D navigation map (heavy Three.js dependency)
const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

const MotionOverlay = motion.create("div");

interface ImageData {
  type: string;
  category: string;
  url: string;
  caption: string;
}

interface NavigationData {
  destination: string;
  floor: string;
  path: number[][];
  path_ids?: string[];
  nodes: any[];
  buildings: any;
}

export function ImageDisplay({
  ignoreNavigation = false,
}: {
  ignoreNavigation?: boolean;
}) {
  // NLU kiosk has no LiveKit SessionProvider — image/nav overlays are handled
  // by kiosk-view itself via the NLU WebSocket action payloads.
  if (process.env.NEXT_PUBLIC_NLU_MODE === "true") {
    return null;
  }
  return (
    <ImageDisplayLiveKit ignoreNavigation={ignoreNavigation} />
  );
}

function ImageDisplayLiveKit({
  ignoreNavigation = false,
}: {
  ignoreNavigation?: boolean;
}) {
  const session = useSessionContext();
  const room = session?.room;
  const [imageData, setImageData] = useState<ImageData | null>(null);
  const [showImage, setShowImage] = useState(false);
  const [navData, setNavData] = useState<NavigationData | null>(null);

  useEffect(() => {
    if (!room) {
      return;
    }

    console.log("✅ ImageDisplay: Setting up data listener");

    const handleDataReceived = (
      payload: Uint8Array,
      participant?: RemoteParticipant,
      kind?: DataPacket_Kind,
    ) => {
      console.log("📨 DATA RECEIVED!", {
        payloadSize: payload.length,
        participantId: participant?.identity,
      });

      try {
        const decoder = new TextDecoder();
        const message = JSON.parse(decoder.decode(payload));

        console.log("📦 Parsed message:", message);

        if (message.type === "image") {
          console.log("📸 Received image URL:", message.url);
          setShowImage(false);
          setNavData(null);
          setTimeout(() => {
            setImageData(message);
            setShowImage(true);
            setTimeout(() => {
              setShowImage(false);
            }, 10000);
          }, 100);
        } else if (message.type === "navigation") {
          if (!ignoreNavigation) {
            console.log("🗺️ Received navigation data:", message.destination);
            setShowImage(false);
            setNavData({
              destination: message.destination,
              floor: message.floor,
              path: message.path || message.path_coords,
              path_ids: message.path_ids || [],
              nodes: message.nodes,
              buildings: message.buildings,
            });
          }
        }
      } catch (error) {
        console.error("❌ Error parsing data message:", error);
      }
    };

    room.on("dataReceived", handleDataReceived);
    console.log("✅ Data listener attached");

    return () => {
      room.off("dataReceived", handleDataReceived);
    };
  }, [room]);

  const handleClose = () => {
    setShowImage(false);
    setTimeout(() => setImageData(null), 500);
  };

  const handleImageClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      handleClose();
    }
  };

  return (
    <>
      {/* Navigation Map Overlay */}
      {navData && (
        <Suspense
          fallback={
            <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80">
              <div className="text-white text-xl animate-pulse">
                Loading 3D Map...
              </div>
            </div>
          }
        >
          <NavigationMap
            path={navData.path}
            path_ids={navData.path_ids}
            nodes={navData.nodes}
            buildings={navData.buildings}
            destination={navData.destination}
            onClose={() => setNavData(null)}
          />
        </Suspense>
      )}

      {/* Image Overlay */}
      <AnimatePresence>
        {showImage && imageData && (
          <MotionOverlay
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            className="fixed inset-0 z-[100] flex items-center justify-center bg-black/90 p-4"
            onClick={handleImageClick}
          >
            <button
              onClick={handleClose}
              className="absolute right-4 top-4 z-[101] flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
              aria-label="Close image"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>

            <motion.div
              initial={{ scale: 0.9, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.9, opacity: 0 }}
              transition={{ duration: 0.3, ease: "easeOut" }}
              className="relative max-h-[90vh] max-w-7xl"
            >
              {imageData.caption && (
                <div className="mb-4 text-center">
                  <h2 className="text-2xl font-semibold text-white md:text-3xl">
                    {imageData.caption}
                  </h2>
                </div>
              )}

              <img
                src={imageData.url}
                alt={imageData.caption || "Display image"}
                className="max-h-[80vh] w-auto rounded-lg object-contain shadow-2xl"
                crossOrigin="anonymous"
              />

              <div className="mt-4 flex justify-center">
                <span
                  className={cn(
                    "rounded-full px-4 py-1 text-sm font-medium",
                    imageData.category === "event" &&
                      "bg-blue-500/20 text-blue-300",
                    imageData.category === "map" &&
                      "bg-green-500/20 text-green-300",
                    imageData.category === "fallback" &&
                      "bg-gray-500/20 text-gray-300",
                  )}
                >
                  {imageData.category === "event" && "🎨 Event"}
                  {imageData.category === "map" && "🗺️ Location"}
                  {imageData.category === "fallback" && "ℹ️ Info"}
                </span>
              </div>
            </motion.div>
          </MotionOverlay>
        )}
      </AnimatePresence>
    </>
  );
}
