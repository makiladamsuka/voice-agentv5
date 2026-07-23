"use client";

import React, { useRef, useMemo, useState, useEffect, useCallback } from "react";
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
  if (lower.includes("lecture hall")) return { color: "#0c4af3ff", icon: "🎓" };
  if (lower.includes("laboratory") || lower.includes("lab"))
    return { color: "#d3c90cff", icon: "🔬" };
  if (lower.includes("office")) return { color: "#166ac3ff", icon: "🏢" };
  if (lower.includes("washroom")) return { color: "#949090", icon: "🚻" };
  if (lower.includes("desk") || lower.includes("evaluator"))
    return { color: "#3730a3", icon: "💁" };
  if (lower.includes("stair")) return { color: "#3d3d3dff", icon: "🪜" }; // structural slate color
  return { color: "#334155", icon: "📍" }; // default sleek slate
};

// Realistic Foundation Staircase Component
const RealisticStaircase = ({ size, isDestination, boxColor }: { size: [number, number, number], isDestination: boolean, boxColor: string }) => {
  const steps = 3;
  // Match the exact grid cell size for a perfect cutout, with a slight overlap (1.02x) to ensure it seals the hole and fits perfectly into the building foundation without seams.
  const width = Math.max(1, size[0]) * 1.5;
  const totalDepth = Math.max(1, size[2]) * 1.02;

  // Foundation is 1.0 unit deep (from y=0 down to y=-1)
  const foundationDepth = 1.0;
  const stepHeight = foundationDepth / steps;
  const stepDepth = totalDepth / steps;

  return (
    // The parent group is positioned at y = size[1]/2. 
    // Shift down by -size[1]/2 so local y=0 is world y=0 (top of the floor).
    <group position={[0, -size[1] / 2, 0]}>
      {Array.from({ length: steps }).map((_, i) => {
        // Render blocks ascending from the bottom of the foundation (y=-1)
        const currentHeight = stepHeight * (i + 1);
        return (
          <mesh
            key={i}
            position={[
              0,
              -foundationDepth + currentHeight / 2, // Base is at y=-1
              totalDepth / 2 - stepDepth / 2 - i * stepDepth // i=0 is front, i=steps-1 is back
            ]}
            castShadow
            receiveShadow
          >
            <boxGeometry args={[width, currentHeight, stepDepth]} />
            <meshStandardMaterial
              color="#3d3d3dff"
              emissive={isDestination ? "#2563EB" : "#000000"}
              emissiveIntensity={isDestination ? 0.4 : 0}
            />
          </mesh>
        );
      })}
    </group>
  );
};

// Animated group container that makes the floor map drop from above or rise from below on change
const AnimatedFloorGroup = ({
  currentFloor,
  destFloor,
  children,
}: {
  currentFloor: string;
  destFloor: string;
  children: React.ReactNode;
}) => {
  const groupRef = useRef<THREE.Group>(null);
  const lastFloorRef = useRef<string>(currentFloor);

  // Spring state refs
  const velocityRef = useRef<number>(0);
  const positionYRef = useRef<number>(0);

  useFrame((_, delta) => {
    if (groupRef.current) {
      if (lastFloorRef.current && lastFloorRef.current !== currentFloor) {
        const lastNum = parseInt(lastFloorRef.current.replace(/\D/g, "")) || 1;
        const currentNum = parseInt(currentFloor.replace(/\D/g, "")) || 1;

        // Reset spring position and velocity on floor change so they always animate in
        const startY = currentNum > lastNum ? 10 : -10;
        groupRef.current.position.y = startY;
        positionYRef.current = startY;
        velocityRef.current = 0;
      }
      lastFloorRef.current = currentFloor;

      // Only run physics equations if there is an active offset to animate
      if (groupRef.current.position.y !== 0) {
        const targetY = 0;
        const tension = 180; // pulling force
        const damping = 12; // Expressive overshoot bounce
        const dt = Math.min(delta, 0.03); // cap delta time to avoid instability on frame drops

        const displacement = positionYRef.current - targetY;
        const springForce = -tension * displacement;
        const dampingForce = -damping * velocityRef.current;
        const acceleration = springForce + dampingForce;

        velocityRef.current += acceleration * dt;
        positionYRef.current += velocityRef.current * dt;

        // Snap to zero if settled
        if (Math.abs(positionYRef.current) < 0.001 && Math.abs(velocityRef.current) < 0.001) {
          positionYRef.current = 0;
          velocityRef.current = 0;
        }

        groupRef.current.position.y = positionYRef.current;
      }
    }
  });

  return <group ref={groupRef}>{children}</group>;
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
  const [highlightedFloor, setHighlightedFloor] = useState<string | null>(null);
  const animTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const cancelAnim = useCallback(() => {
    animTimersRef.current.forEach(clearTimeout);
    animTimersRef.current = [];
    setHighlightedFloor(null);
  }, []);

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

  const destNode = useMemo(() => {
    return nodes.find(
      (n: any) =>
        n.label?.toLowerCase() === destination.toLowerCase() &&
        n.type !== "waypoint",
    );
  }, [nodes, destination]);

  const startFloorKey = useMemo(() => {
    return floorSequence[0] || (availableFloors.length > 0 ? availableFloors[availableFloors.length - 1] : "floor_1");
  }, [floorSequence, availableFloors]);

  const destFloorKey = useMemo(() => {
    return floorSequence[floorSequence.length - 1] || destNode?.floor || "floor_1";
  }, [floorSequence, destNode]);

  useEffect(() => {
    if (nodes.length === 0) return;

    const startNum = parseInt(startFloorKey.replace(/\D/g, "")) || 1;
    const destNum = parseInt(destFloorKey.replace(/\D/g, "")) || 1;

    // Clear any existing animation before starting a new one
    cancelAnim();

    // Map goes straight to the relevant destination floor on load
    setCurrentFloor(destFloorKey);

    // Initialize highlighted floor at start floor
    setHighlightedFloor(startFloorKey);

    if (startNum === destNum) return;

    const sequence: { floor: string; delay: number }[] = [];
    let currentDelay = 450; // Show starting highlighted button floor for 0.45 seconds

    if (startNum < destNum) {
      for (let f = startNum + 1; f <= destNum; f++) {
        sequence.push({ floor: `floor_${f}`, delay: currentDelay });
        currentDelay += 450; // Step highlight on each intermediate floor button
      }
    } else {
      for (let f = startNum - 1; f >= destNum; f--) {
        sequence.push({ floor: `floor_${f}`, delay: currentDelay });
        currentDelay += 450; // Step highlight on each intermediate floor button
      }
    }

    const timers = sequence.map((step) => {
      return setTimeout(() => {
        setHighlightedFloor(step.floor);
      }, step.delay);
    });

    animTimersRef.current = timers;

    return () => {
      timers.forEach(clearTimeout);
    };
  }, [nodes, floorSequence, destNode, availableFloors, cancelAnim, startFloorKey, destFloorKey]);

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
          <div className="text-[11px] font-bold text-center uppercase tracking-wider text-white/70 py-1">
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
            const isButtonActive = highlightedFloor ? highlightedFloor === f : active;

            return (
              <button
                key={`${f}-${idx}`}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  cancelAnim();
                  setCurrentFloor(f as string);
                }}
                className={`w-full min-h-[52px] px-3 py-2.5 rounded-3xl font-bold flex flex-col items-center justify-center border transition-all duration-300 shadow-lg backdrop-blur-md overflow-hidden ${isButtonActive
                  ? "bg-blue-500/80 text-white border-blue-400/30"
                  : "bg-black/60 text-white border-white/10"
                  }`}
              >
                <span className="text-[14px] leading-tight">
                  {(f as string).replace("floor_", "Floor ")}
                </span>
                {badge ? (
                  <span
                    className={`text-[10px] mt-0.5 font-semibold leading-tight ${isButtonActive ? "opacity-85 text-white" : "opacity-70 text-white"
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
              cancelAnim();
              setCurrentFloor(availableFloors[currentIndex - 1] as string);
              lastScrollRef.current = now;
            }
          } else if (e.deltaY > 0) {
            // Scroll down -> Go to lower floor (higher index)
            if (currentIndex < availableFloors.length - 1) {
              cancelAnim();
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

            <AnimatedFloorGroup currentFloor={currentFloor} destFloor={destFloorKey}>
              {/* Building Grids */}
              {buildingEntries.map(([bId, b]) => {
                // Find all stair nodes on this floor to cut holes in the foundation
                const stairNodes = floorNodes.filter((n) => n.label?.toLowerCase().includes("stair"));

                return (
                  <group key={bId} position={b.position}>
                    {/* Floor Cells (skipping removed cells and stair cutouts) */}
                    {Array.from({ length: Math.round(b.size[0] || 1) }).map(
                      (_, c) =>
                        Array.from({ length: Math.round(b.size[1] || 1) }).map(
                          (_, r) => {
                            const cellId = `${c}_${r}`;
                            if (b.removed_cells?.includes(cellId)) return null;
                            const cx = c - b.size[0] / 2 + 0.5;
                            const cz = r - b.size[1] / 2 + 0.5;

                            // Calculate world coordinates for the cell
                            const cellWorldX = (b.position?.[0] || 0) + cx;
                            const cellWorldZ = (b.position?.[2] || 0) + cz;

                            // Skip rendering this cell if a stair node sits on it
                            const isUnderStair = stairNodes.some((n) => {
                              const sx = n.world[0];
                              const sz = n.world[2];
                              const w = n.size?.[0] || 1;
                              const d = n.size?.[2] || 1;
                              return Math.abs(cellWorldX - sx) < w / 2 && Math.abs(cellWorldZ - sz) < d / 2;
                            });
                            if (isUnderStair) return null;

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
                      position={[0, 1, b.size[1] / 2 + 0.5]}
                      rotation={[-Math.PI / 2, 0, 0]}
                      fontSize={0.6}
                      color="#000000"
                      fontWeight="bold"
                      textAlign="center"
                    >
                      {b.name.replace(" ", "\n")}
                    </Text>
                  </group>
                );
              })}

              {/* Room Blocks */}
              {floorNodes.map((node: any, index: number) => {
                const size = node.size || [1, 1, 1];
                const isDestination =
                  node.label?.toLowerCase() === destination.toLowerCase();
                const theme = getRoomTheme(node.label);
                // Use a vibrant indigo for the destination instead of green
                // If it's a staircase, always force the theme color to ignore server overrides
                const isStair = node.label?.toLowerCase().includes("stair");
                const boxColor = isDestination ? "#2563EB" : (isStair ? theme.color : (node.color || theme.color));

                // Elevate ALL labels and alternate heights to prevent crossing
                const staggerHeight = 1.0 + (index % 2) * 0.8;
                const textY = size[1] / 2 + staggerHeight;

                const isStaircase = node.label?.toLowerCase().includes("stair");

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
                    {isStaircase ? (
                      <RealisticStaircase size={size} isDestination={isDestination} boxColor={boxColor} />
                    ) : (
                      <Box args={size} castShadow>
                        <meshStandardMaterial
                          color={boxColor}
                          emissive={isDestination ? "#2563EB" : "#000000"}
                          emissiveIntensity={isDestination ? 0.4 : 0}
                        />
                      </Box>
                    )}

                    {/* Text label painted directly on the top of the item */}
                    {!isStaircase && (
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
                    )}
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
            </AnimatedFloorGroup>

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
