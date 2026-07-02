"use client";

import React, { useRef, useMemo } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Grid, Box, Text, Line } from "@react-three/drei";
import * as THREE from "three";

// Animated path for edges
const EdgePath = ({ points }: { points: [number, number, number][] }) => {
  const lineRef = useRef<any>(null);
  useFrame((_, delta) => {
    if (lineRef.current?.material) {
      lineRef.current.material.dashOffset -= delta * 1.5;
    }
  });
  if (points.length < 2) return null;
  return (
    <Line
      ref={lineRef}
      points={points}
      color="#ef4444"
      lineWidth={3}
      dashed
      dashSize={0.4}
      gapSize={0.3}
    />
  );
};

interface CampusMapEmbedProps {
  mapData: any; // { nodes, edges, buildings }
}

export default function CampusMapEmbed({ mapData }: CampusMapEmbedProps) {
  const buildings = mapData?.buildings || {};
  const nodes = mapData?.nodes || [];
  const edges = mapData?.edges || [];

  // Compute world positions for each node
  const nodePositions = useMemo(() => {
    const positions: Record<string, [number, number, number]> = {};
    for (const node of nodes) {
      const b = buildings[node.building] || { position: [0, 0, 0] };
      const wx = b.position[0] + node.x;
      const wz = b.position[2] + node.z;
      if (node.type === "waypoint") {
        positions[node.id] = [wx, 0.1, wz];
      } else {
        const h = node.size ? node.size[1] / 2 : 0.5;
        positions[node.id] = [wx, h, wz];
      }
    }
    return positions;
  }, [nodes, buildings]);

  if (!mapData) {
    return (
      <div className="w-full h-full flex items-center justify-center text-on-surface-variant/40 text-sm">
        <span className="material-symbols-outlined text-3xl mr-2 opacity-30">
          map
        </span>
        No map data
      </div>
    );
  }

  return (
    <Canvas shadows orthographic camera={{ position: [20, 20, 20], zoom: 22 }}>
      <ambientLight intensity={0.6} />
      <directionalLight position={[10, 20, 10]} intensity={0.8} castShadow />

      {/* Building Grids */}
      {(Object.entries(buildings) as [string, any][]).map(([bId, b]) => (
        <group key={bId} position={b.position}>
          <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
            <planeGeometry args={[b.size[0], b.size[1]]} />
            <meshStandardMaterial color={b.color} transparent opacity={0.7} />
          </mesh>
          <Grid
            args={[b.size[0], b.size[1]]}
            position={[0, 0.01, 0]}
            cellSize={1}
            cellThickness={1}
            cellColor="#555"
            sectionSize={1}
            sectionThickness={1.5}
            sectionColor="#333"
            fadeDistance={40}
          />
          <Text
            position={[0, 0.02, b.size[1] / 2 + 0.5]}
            rotation={[-Math.PI / 2, 0, 0]}
            fontSize={0.6}
            color={b.color}
          >
            {b.name}
          </Text>
        </group>
      ))}

      {/* Room Blocks */}
      {nodes
        .filter((n: any) => n.type !== "waypoint")
        .map((node: any) => {
          const pos = nodePositions[node.id];
          if (!pos) return null;
          const size = node.size || [1, 1, 1];
          return (
            <group key={node.id} position={pos}>
              <Box args={size} castShadow>
                <meshStandardMaterial
                  color="#334155"
                  transparent
                  opacity={0.9}
                />
              </Box>
              <Text
                position={[0, size[1] / 2 + 0.35, 0]}
                fontSize={0.3}
                color="#ffffff"
                anchorX="center"
                anchorY="middle"
              >
                {node.label}
              </Text>
            </group>
          );
        })}

      {/* Waypoints (small teal discs) */}
      {nodes
        .filter((n: any) => n.type === "waypoint")
        .map((node: any) => {
          const pos = nodePositions[node.id];
          if (!pos) return null;
          return (
            <mesh key={node.id} position={pos} rotation={[-Math.PI / 2, 0, 0]}>
              <circleGeometry args={[0.2, 16]} />
              <meshStandardMaterial color="#14b8a6" transparent opacity={0.6} />
            </mesh>
          );
        })}

      {/* Edges (paths) */}
      {edges.map((edge: any) => {
        const p1 = nodePositions[edge.source];
        const p2 = nodePositions[edge.target];
        if (!p1 || !p2) return null;
        const points: [number, number, number][] = [
          [p1[0], 0.15, p1[2]],
          [p2[0], 0.15, p2[2]],
        ];
        return <EdgePath key={edge.id} points={points} />;
      })}

      <OrbitControls
        enableZoom={true}
        enablePan={true}
        maxPolarAngle={Math.PI / 2 - 0.1}
        autoRotate
        autoRotateSpeed={0.5}
      />
    </Canvas>
  );
}
