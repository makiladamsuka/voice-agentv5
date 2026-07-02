"use client";

import React, { useRef, useMemo, useState, useEffect } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import {
  OrbitControls,
  Line,
  RoundedBox,
  Grid,
  Text,
  Sparkles,
  Environment,
} from "@react-three/drei";
import * as THREE from "three";

// Animated glowing dashed path that flows toward the destination
const GlowingPath = ({ points }: { points: [number, number, number][] }) => {
  const lineRef = useRef<any>(null);
  const orbRef = useRef<THREE.Mesh>(null);

  // Create a curve from points for the moving orb
  const curve = useMemo(() => {
    if (points.length < 2) return null;
    return new THREE.CatmullRomCurve3(
      points.map((p) => new THREE.Vector3(...p)),
    );
  }, [points]);

  useFrame((state, delta) => {
    if (lineRef.current?.material) {
      lineRef.current.material.dashOffset -= delta * 2;
    }

    // Animate glowing orb along the path
    if (orbRef.current && curve) {
      const time = (state.clock.elapsedTime * 0.15) % 1; // Speed of the orb
      const pos = curve.getPointAt(time);
      orbRef.current.position.copy(pos);
    }
  });

  if (points.length < 2) return null;

  return (
    <group>
      <Line
        ref={lineRef}
        points={points}
        color="#ef4444"
        lineWidth={8}
        dashed
        dashSize={0.5}
        gapSize={0.3}
      />
      {/* Moving energy orb */}
      <mesh ref={orbRef}>
        <sphereGeometry args={[0.4, 32, 32]} />
        <meshBasicMaterial color="#ffaaaa" />
        <pointLight color="#ef4444" intensity={2} distance={3} />
      </mesh>
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
          color="#22c55e"
          transparent
          opacity={0.5}
          side={THREE.DoubleSide}
        />
      </mesh>
      {/* Floating marker */}
      <mesh ref={meshRef} position={position}>
        <octahedronGeometry args={[0.3]} />
        <meshStandardMaterial
          color="#22c55e"
          emissive="#22c55e"
          emissiveIntensity={0.8}
        />
      </mesh>
      {/* Label */}
      <Text
        position={[position[0], position[1] + 1.5, position[2]]}
        fontSize={0.5}
        color="#22c55e"
        anchorX="center"
        anchorY="middle"
        fontWeight="bold"
      >
        📍 {label}
      </Text>
    </group>
  );
};

interface NavigationMapProps {
  path: number[][]; // Array of [x, y, z] world coordinates
  nodes: any[]; // All nodes from the map
  buildings: any; // Building positions/sizes
  destination: string; // Destination label
  onClose?: () => void; // Close callback
  inline?: boolean; // If true, render without fullscreen overlay styles
}

export default function NavigationMap({
  path,
  nodes,
  buildings,
  destination,
  onClose,
  inline = false,
}: NavigationMapProps) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    // Animate in
    setTimeout(() => setVisible(true), 50);

    // Auto-dismiss after 20 seconds
    const timer = setTimeout(() => {
      setVisible(false);
      setTimeout(() => onClose?.(), 400);
    }, 20000);

    return () => clearTimeout(timer);
  }, [onClose]);

  const handleClose = () => {
    setVisible(false);
    setTimeout(() => onClose?.(), 400);
  };

  const pathPoints = useMemo(() => {
    return path.map((p) => [p[0], 0.3, p[2]] as [number, number, number]);
  }, [path]);

  const destNode = useMemo(() => {
    return nodes.find(
      (n: any) =>
        n.label?.toLowerCase() === destination.toLowerCase() &&
        n.type !== "waypoint",
    );
  }, [nodes, destination]);

  const buildingEntries = useMemo(() => {
    return Object.entries(buildings || {}) as [string, any][];
  }, [buildings]);

  return (
    <div
      className={
        inline
          ? "w-full h-full relative"
          : `fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex flex-col items-center justify-center transition-all duration-500 ${
              visible ? "opacity-100 scale-100" : "opacity-0 scale-95"
            }`
      }
      onClick={!inline ? handleClose : undefined}
    >
      {/* Header */}
      {!inline && (
        <div
          className="absolute top-6 left-0 right-0 flex justify-center z-10"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="bg-gray-900/90 border border-gray-700 rounded-2xl px-8 py-4 flex items-center gap-4 shadow-2xl">
            <div className="w-3 h-3 bg-red-500 rounded-full animate-pulse" />
            <span className="text-white text-lg font-bold">
              Navigating to: {destination}
            </span>
            <button
              onClick={handleClose}
              className="ml-4 text-gray-400 hover:text-white text-2xl font-bold"
            >
              &times;
            </button>
          </div>
        </div>
      )}

      {/* 3D Canvas */}
      <div className="w-full h-full" onClick={(e) => e.stopPropagation()}>
        <Canvas
          shadows
          orthographic
          camera={{ position: [20, 20, 20], zoom: 35 }}
        >
          <ambientLight intensity={0.6} />
          <directionalLight position={[10, 20, 10]} intensity={1} castShadow />
          <pointLight
            position={[0, 5, 0]}
            intensity={0.5}
            color="#ef4444"
            distance={15}
          />

          {/* Environment Ambiance */}
          <Sparkles
            count={200}
            scale={30}
            size={1.5}
            speed={0.4}
            opacity={0.2}
            color="#818cf8"
          />

          {/* Building Grids */}
          {buildingEntries.map(([bId, b]) => (
            <group key={bId} position={b.position}>
              <mesh
                rotation={[-Math.PI / 2, 0, 0]}
                receiveShadow
                position={[0, -0.05, 0]}
              >
                <planeGeometry args={[b.size[0] + 2, b.size[1] + 2]} />
                <meshStandardMaterial
                  color={b.color}
                  transparent
                  opacity={0.15}
                />
              </mesh>
              {/* Removed Grid based on user request */}
              <Text
                position={[0, 0.02, b.size[1] / 2 + 0.5]}
                rotation={[-Math.PI / 2, 0, 0]}
                fontSize={0.7}
                color={b.color}
              >
                {b.name}
              </Text>
            </group>
          ))}

          {/* Room Blocks (Glassmorphism & Color Coded) */}
          {nodes
            .filter(
              (n: any) =>
                n.type !== "waypoint" &&
                n.floor === (destNode?.floor || "floor_1"),
            )
            .map((node: any) => {
              const size = node.size || [1, 1, 1];
              const isDestination =
                node.label?.toLowerCase() === destination.toLowerCase();

              // Determine color based on room type
              let roomColor = "#3b82f6"; // Default Blue
              const lbl = (node.label || "").toLowerCase();
              if (lbl.includes("lab") || lbl.includes("laboratory"))
                roomColor = "#c084fc"; // Purple for Labs
              else if (
                lbl.includes("lec") ||
                lbl.includes("hall") ||
                lbl.includes("auditorium")
              )
                roomColor = "#fb923c"; // Orange for Lecture Halls
              else if (
                lbl.includes("dept") ||
                lbl.includes("department") ||
                lbl.includes("office") ||
                lbl.includes("unit") ||
                lbl.includes("desk")
              )
                roomColor = "#38bdf8"; // Light Blue for Departments
              else if (lbl.includes("washroom") || lbl.includes("restroom"))
                roomColor = "#94a3b8"; // Slate for Washrooms

              return (
                <group
                  key={node.id}
                  position={[node.world[0], node.world[1], node.world[2]]}
                >
                  <RoundedBox
                    args={size}
                    radius={0.1}
                    smoothness={4}
                    castShadow
                  >
                    {isDestination ? (
                      <meshStandardMaterial
                        color="#22c55e"
                        emissive="#22c55e"
                        emissiveIntensity={0.5}
                        transparent
                        opacity={0.9}
                      />
                    ) : (
                      <meshPhysicalMaterial
                        color={roomColor}
                        transmission={0.8}
                        opacity={1}
                        metalness={0}
                        roughness={0.1}
                        ior={1.5}
                        thickness={0.5}
                      />
                    )}
                  </RoundedBox>
                  <Text
                    position={[0, size[1] / 2 + 0.4, 0]}
                    fontSize={0.35}
                    color={isDestination ? "#22c55e" : "#cbd5e1"}
                    anchorX="center"
                    anchorY="middle"
                    outlineWidth={0.02}
                    outlineColor="#000000"
                  >
                    {node.label}
                  </Text>
                </group>
              );
            })}

          {/* Glowing Animated Path */}
          {pathPoints.length >= 2 && <GlowingPath points={pathPoints} />}

          {/* Destination Marker */}
          {destNode && (
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
            enableZoom={true}
            enablePan={true}
            maxPolarAngle={Math.PI / 2 - 0.1}
            autoRotate
            autoRotateSpeed={1.5}
          />
        </Canvas>
      </div>

      {/* Bottom hint */}
      {!inline && (
        <div className="absolute bottom-6 left-0 right-0 flex justify-center z-10">
          <p className="text-gray-400 text-sm">
            Tap anywhere to close • Auto-closes in 20 seconds
          </p>
        </div>
      )}
    </div>
  );
}
