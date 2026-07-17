"use client";

import React, { useRef, useMemo, useState, useEffect } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Line, Box, Grid, Text } from "@react-three/drei";
import * as THREE from "three";

// Animated glowing path that flows toward the destination
const GlowingPath = ({ points }: { points: [number, number, number][] }) => {
  const lineRef = useRef<any>(null);

  // Calculate approximate path length to scale the shimmer appropriately
  const pathLength = useMemo(() => {
    let len = 0;
    for (let i = 0; i < points.length - 1; i++) {
      const dx = points[i + 1][0] - points[i][0];
      const dy = points[i + 1][1] - points[i][1];
      const dz = points[i + 1][2] - points[i][2];
      len += Math.sqrt(dx * dx + dy * dy + dz * dz);
    }
    return len;
  }, [points]);

  useFrame((_, delta) => {
    if (lineRef.current?.material) {
      // Fast sweeping motion toward the destination
      lineRef.current.material.dashOffset -= delta * 12;
    }
  });

  if (points.length < 2) return null;

  // The highlight (shimmer) length
  const dashSize = Math.max(1, pathLength * 0.05); // Shimmer length
  // The gap is large so the shimmer looks like a discrete moving pulse
  const gapSize = Math.max(12, pathLength * 0.85);

  return (
    <group>
      {/* Base track: continuous solid line (progress bar base) */}
      <Line
        points={points}
        color="#1D4ED8" // brand blue base
        lineWidth={18}
        transparent
        opacity={0.8}
      />
      {/* Pulse track: bright sweeping shimmer (leading edge glow) */}
      <Line
        ref={lineRef}
        points={points}
        color="#60A5FA" // light brand pulse
        lineWidth={18}
        transparent
        opacity={0.9}
        dashed
        dashSize={dashSize}
        gapSize={gapSize}
      />
    </group>
  );
};

// Pulsing destination marker
const DestinationMarker = ({
  position,
  label,
}: {
  position: [number, number, number];
  label: string;
}) => {
  const meshRef = useRef<THREE.Mesh>(null);
  const ringRef = useRef<THREE.Mesh>(null);

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    if (meshRef.current) {
      meshRef.current.position.y = position[1] + 0.3 + Math.sin(t * 3) * 0.2;
    }
    if (ringRef.current) {
      ringRef.current.scale.setScalar(1 + Math.sin(t * 2) * 0.3);
      (ringRef.current.material as THREE.MeshBasicMaterial).opacity =
        0.4 + Math.sin(t * 2) * 0.2;
    }
  });

  return (
    <group>
      {/* Pulsing ring on the ground */}
      <mesh
        ref={ringRef}
        position={[position[0], 0.05, position[2]]}
        rotation={[-Math.PI / 2, 0, 0]}
      >
        <ringGeometry args={[0.6, 0.9, 32]} />
        <meshBasicMaterial
          color="#10B981"
          transparent
          opacity={0.5}
          side={THREE.DoubleSide}
        />
      </mesh>
      {/* Floating marker */}
      <mesh ref={meshRef} position={position}>
        <octahedronGeometry args={[0.3]} />
        <meshStandardMaterial
          color="#10B981"
          emissive="#10B981"
          emissiveIntensity={0.8}
        />
      </mesh>
      {/* Label */}
      <Text
        position={[position[0], position[1] + 1.5, position[2]]}
        fontSize={0.5}
        color="#059669"
        anchorX="center"
        anchorY="middle"
        fontWeight="bold"
      >
        {label}
      </Text>
    </group>
  );
};

interface NavigationMapProps {
  path: number[][]; // Array of [x, y, z] world coordinates
  path_ids?: string[]; // Array of node IDs in the path
  directions?: string; // Optional directions text
  nodes: any[]; // All nodes from the map
  buildings: any; // Building positions/sizes
  edges?: any[]; // Edges to render all connections
  destination: string; // Destination label
  onClose?: () => void; // Close callback
  isStandalone?: boolean; // Standalone mode for 3d-map page
  isManualExpanded?: boolean; // Manual expansion mode
  onNodeClick?: (label: string) => void; // Callback when a node is clicked
  hideFloorSwitcher?: boolean; // Hide floor switcher UI
}

// Updated to cohesive, elegant dark-theme colors
const getRoomTheme = (label: string) => {
  if (!label) return { color: "#334155", icon: "📍" };
  const lower = label.toLowerCase();
  if (lower.includes("lecture hall")) return { color: "#1e3a8a", icon: "🎓" };
  if (lower.includes("laboratory") || lower.includes("lab"))
    return { color: "#4c1d95", icon: "🔬" };
  if (lower.includes("office")) return { color: "#0f766e", icon: "🏢" };
  if (lower.includes("washroom")) return { color: "#115e59", icon: "🚻" };
  if (lower.includes("desk") || lower.includes("evaluator"))
    return { color: "#3730a3", icon: "💁" };
  return { color: "#334155", icon: "📍" }; // default sleek slate
};

export default function NavigationMap({
  path = [],
  path_ids = [],
  nodes = [],
  buildings = {},
  edges = [],
  destination = "Destination",
  onClose,
  isStandalone = false,
  isManualExpanded = false,
  onNodeClick,
  hideFloorSwitcher = false,
}: Partial<NavigationMapProps>) {
  const [visible, setVisible] = useState(false);
  const [currentFloor, setCurrentFloor] = useState<string>("");
  const lastScrollRef = useRef<number>(0);

  const availableFloors = useMemo(() => {
    return Array.from(
      new Set(nodes.map((n) => n.floor).filter(Boolean)),
    ).sort((a, b) => {
      const numA = parseInt((a as string).replace(/\D/g, "")) || 0;
      const numB = parseInt((b as string).replace(/\D/g, "")) || 0;
      return numB - numA;
    });
  }, [nodes]);

  const floorSequence = useMemo(() => {
    if (!path_ids || path_ids.length === 0) return [];
    const seq: string[] = [];
    path_ids.forEach((id) => {
      const floorStr = id?.split("::")[0];
      if (floorStr && seq[seq.length - 1] !== floorStr) {
        seq.push(floorStr);
      }
    });
    return seq;
  }, [path_ids]);

  useEffect(() => {
    if (!currentFloor) {
      if (floorSequence.length > 0) {
        setCurrentFloor(floorSequence[0] as string);
      } else if (availableFloors.length > 0) {
        // default to lowest floor (Floor 1)
        setCurrentFloor(availableFloors[availableFloors.length - 1] as string);
      }
    }
  }, [floorSequence, currentFloor, availableFloors]);

  useEffect(() => {
    // Animate in
    setTimeout(() => setVisible(true), 50);

    if (!isStandalone && !isManualExpanded) {
      // Auto-dismiss after 20 seconds
      const timer = setTimeout(() => {
        setVisible(false);
        setTimeout(() => onClose?.(), 400);
      }, 20000);

      return () => clearTimeout(timer);
    }
  }, [onClose, isStandalone, isManualExpanded]);

  const handleClose = () => {
    setVisible(false);
    setTimeout(() => onClose?.(), 400);
  };

  const pathPoints = useMemo(() => {
    if (!path || path.length === 0) return [];
    // Without floor-scoped path_ids, show the whole path on every floor.
    const hasIds = Array.isArray(path_ids) && path_ids.length === path.length;
    const points: [number, number, number][] = [];
    for (let i = 0; i < path.length; i++) {
      if (hasIds) {
        const floorStr = path_ids[i]?.split("::")[0];
        if (floorStr !== currentFloor) continue;
      }
      points.push([path[i][0], 0.3, path[i][2]]);
    }
    return points;
  }, [path, path_ids, currentFloor]);

  const destNode = useMemo(() => {
    return nodes.find(
      (n: any) =>
        n.label?.toLowerCase() === destination.toLowerCase() &&
        n.type !== "waypoint",
    );
  }, [nodes, destination]);

  const buildingEntries = useMemo(() => {
    return Object.entries(buildings || {}).filter(
      ([_, b]: [string, any]) => !b.floor || b.floor === currentFloor,
    ) as [string, any][];
  }, [buildings, currentFloor]);

  const floorNodes = useMemo(() => {
    return nodes.filter(
      (n) => n.floor === currentFloor && n.type !== "waypoint",
    );
  }, [nodes, currentFloor]);

  return (
    <div
      className={
        isStandalone || isManualExpanded
          ? `w-full h-full bg-transparent flex flex-col items-center justify-center transition-all duration-500 overflow-hidden ${visible ? "opacity-100 scale-100" : "opacity-0 scale-95"}`
          : `absolute inset-0 z-50 bg-surface/90 backdrop-blur-md rounded-[32px] overflow-hidden shadow-2xl flex flex-col items-center justify-center transition-all duration-500 ${visible ? "opacity-100 scale-100" : "opacity-0 scale-95"}`
      }
      onClick={isStandalone || isManualExpanded ? undefined : handleClose}
    >
      {/* Header */}
      {!isStandalone && !isManualExpanded && (
        <div
          className="absolute top-6 left-0 right-0 flex justify-center z-10"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="bg-surface-container-highest border border-outline-variant/30 rounded-full px-8 py-4 flex items-center gap-4 shadow-2xl">
            <div className="w-3 h-3 bg-red-500 rounded-full animate-pulse" />
            <span className="text-on-surface text-lg font-bold">
              Navigating to: {destination}
            </span>
            <button
              onClick={handleClose}
              className="ml-4 text-on-surface-variant hover:text-on-surface text-2xl font-bold transition-colors"
            >
              &times;
            </button>
          </div>
        </div>
      )}

      {/* Floor Switcher — even pills matching kiosk chrome */}
      {!hideFloorSwitcher && (
        <div
          className="absolute left-4 top-1/2 -translate-y-1/2 flex flex-col gap-2 z-20 w-[108px] max-h-[70vh] overflow-y-auto pointer-events-auto [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="text-[11px] font-bold text-center uppercase tracking-wider text-[var(--kiosk-muted,#6b7280)] py-1">
            Floors
          </div>
          {availableFloors.map((f, idx) => {
            let badge = "";
            if (!isStandalone && floorSequence.length > 0) {
              if (floorSequence.length === 1 && f === floorSequence[0]) {
                badge = "Start & Dest";
              } else if (f === floorSequence[0]) {
                badge = "Start";
              } else if (f === floorSequence[floorSequence.length - 1]) {
                badge = "Dest";
              } else if (floorSequence.includes(f as string)) {
                badge = "Route";
              }
            }
            const active = currentFloor === f;

            return (
              <button
                key={`${f}-${idx}`}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setCurrentFloor(f as string);
                }}
                className={`w-full min-h-[52px] px-3 py-2.5 rounded-2xl font-bold flex flex-col items-center justify-center border transition-colors ${
                  active
                    ? "bg-[var(--kiosk-brand,#2563eb)] text-[var(--kiosk-brand-fg,#fff)] border-[var(--kiosk-brand,#2563eb)]"
                    : "bg-[var(--kiosk-surface,#fff)] text-[var(--kiosk-text,#111827)] border-[var(--kiosk-border,#e5e7eb)]"
                }`}
              >
                <span className="text-[14px] leading-tight">
                  {(f as string).replace("floor_", "Floor ")}
                </span>
                {badge ? (
                  <span
                    className={`text-[10px] mt-0.5 font-semibold leading-tight ${
                      active ? "opacity-85" : "text-[var(--kiosk-muted,#6b7280)]"
                    }`}
                  >
                    {badge}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      )}

      {/* 3D Canvas */}
      <div 
        className="w-full h-full" 
        onClick={(e) => e.stopPropagation()}
        onWheel={(e) => {
          e.stopPropagation();
          const now = Date.now();
          if (now - lastScrollRef.current < 400) return; // 400ms cooldown

          if (!availableFloors.length) return;
          const currentIndex = availableFloors.indexOf(currentFloor);
          if (currentIndex === -1) return;

          if (e.deltaY < 0) {
            // Scroll up -> Go to higher floor (lower index since it's sorted descending)
            if (currentIndex > 0) {
              setCurrentFloor(availableFloors[currentIndex - 1] as string);
              lastScrollRef.current = now;
            }
          } else if (e.deltaY > 0) {
            // Scroll down -> Go to lower floor (higher index)
            if (currentIndex < availableFloors.length - 1) {
              setCurrentFloor(availableFloors[currentIndex + 1] as string);
              lastScrollRef.current = now;
            }
          }
        }}
      >
        <Canvas
          shadows
          orthographic
          camera={{ position: [20, 20, 20], zoom: isStandalone ? 35 : 28 }}
        >
          <React.Suspense fallback={null}>
            <ambientLight intensity={0.6} />
            <directionalLight
              position={[10, 20, 10]}
              intensity={1}
              castShadow
            />
            <pointLight
              position={[0, 5, 0]}
              intensity={0.35}
              color="#93C5FD"
              distance={15}
            />

            {/* Building Grids */}
            {buildingEntries.map(([bId, b]) => (
              <group key={bId} position={b.position}>
                {/* Floor Cells (skipping removed cells) */}
                {Array.from({ length: Math.round(b.size[0] || 1) }).map(
                  (_, c) =>
                    Array.from({ length: Math.round(b.size[1] || 1) }).map(
                      (_, r) => {
                        const cellId = `${c}_${r}`;
                        if (b.removed_cells?.includes(cellId)) return null;
                        const cx = c - b.size[0] / 2 + 0.5;
                        const cz = r - b.size[1] / 2 + 0.5;
                        return (
                          <mesh
                            key={cellId}
                            position={[cx, -0.5, cz]}
                            receiveShadow
                          >
                            <boxGeometry args={[1, 1, 1]} />
                            <meshStandardMaterial color={b.color} />
                          </mesh>
                        );
                      },
                    ),
                )}
                <Text
                  position={[0, 0.02, b.size[1] / 2 + 0.5]}
                  rotation={[-Math.PI / 2, 0, 0]}
                  fontSize={0.7}
                  color={b.color}
                  fontWeight="bold"
                  textAlign="center"
                >
                  {b.name.replace(" ", "\n")}
                </Text>
              </group>
            ))}

            {/* Room Blocks */}
            {floorNodes.map((node: any, index: number) => {
              const size = node.size || [1, 1, 1];
              const isDestination =
                node.label?.toLowerCase() === destination.toLowerCase();
              const theme = getRoomTheme(node.label);
              // Use a vibrant indigo for the destination instead of green
              const boxColor = isDestination ? "#2563EB" : theme.color;

              // Elevate ALL labels and alternate heights to prevent crossing
              const staggerHeight = 1.0 + (index % 2) * 0.8;
              const textY = size[1] / 2 + staggerHeight;

              return (
                <group
                  key={node.id}
                  position={[node.world[0], size[1] / 2, node.world[2]]}
                  onClick={(e) => {
                    if (onNodeClick) {
                      e.stopPropagation();
                      onNodeClick(node.label);
                    }
                  }}
                  onPointerOver={(e) => {
                    if (onNodeClick) {
                      e.stopPropagation();
                      document.body.style.cursor = "pointer";
                    }
                  }}
                  onPointerOut={(e) => {
                    if (onNodeClick) {
                      e.stopPropagation();
                      document.body.style.cursor = "auto";
                    }
                  }}
                >
                  <Box args={size} castShadow>
                    <meshStandardMaterial
                      color={boxColor}
                      emissive={isDestination ? "#2563EB" : "#000000"}
                      emissiveIntensity={isDestination ? 0.4 : 0}
                    />
                  </Box>

                  {/* Text label painted directly on the top of the item */}
                  <Text
                    position={[0, size[1] / 2 + 0.05, 0]}
                    rotation={[-Math.PI / 2, 0, 0]}
                    fontSize={0.35}
                    color="#ffffff"
                    anchorX="center"
                    anchorY="middle"
                    fontWeight="bold"
                    textAlign="center"
                    lineHeight={1.1}
                  >
                    {`${theme.icon}\n${node.label.replace(" ", "\n")}`}
                  </Text>
                </group>
              );
            })}

            {/* Glowing route — navigation only, never in explore mode */}
            {!isStandalone && pathPoints.length >= 2 && (
              <GlowingPath points={pathPoints} />
            )}

            {/* Destination marker — navigation only */}
            {!isStandalone &&
              destNode &&
              destNode.floor === currentFloor && (
              <DestinationMarker
                position={[
                  destNode.world[0],
                  destNode.world[1],
                  destNode.world[2],
                ]}
                label={destination}
              />
            )}

            <OrbitControls
              enableZoom={false}
              enablePan={true}
              maxPolarAngle={Math.PI / 2 - 0.1}
              target={[0, 0, 0]}
            />
          </React.Suspense>
        </Canvas>
      </div>

      {/* Bottom hint */}
      {!isStandalone && !isManualExpanded && (
        <div className="absolute bottom-6 left-0 right-0 flex justify-center z-10">
          <p className="text-gray-400 text-sm">
            Tap anywhere to close • Auto-closes in 20 seconds
          </p>
        </div>
      )}
    </div>
  );
}
