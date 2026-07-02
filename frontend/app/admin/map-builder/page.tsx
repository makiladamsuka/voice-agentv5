"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */

import React, { useState, useRef, useEffect } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, Line, Box, Grid, Text } from "@react-three/drei";

type Node3D = {
  id: string;
  x: number;
  z: number;
  building: "building_1" | "building_2";
  label: string;
  size?: [number, number, number];
  type?: "room" | "waypoint";
};
type Edge3D = { id: string; source: string; target: string };

const BLOCK_SIZE = 1;

const AnimatedPath = ({
  points,
  onClick,
  onDoubleClick,
  isSelected,
}: {
  points: [number, number, number][];
  onClick?: any;
  onDoubleClick?: any;
  isSelected?: boolean;
}) => {
  const lineRef = useRef<any>(null);
  useFrame((_, delta) => {
    if (lineRef.current?.material) {
      lineRef.current.material.dashOffset -= delta * 2;
    }
  });
  if (points.length < 2) return null;
  return (
    <mesh onClick={onClick} onDoubleClick={onDoubleClick}>
      <Line
        ref={lineRef}
        points={points}
        color={isSelected ? "#facc15" : "#ef4444"}
        lineWidth={isSelected ? 8 : 4}
        dashed
        dashSize={0.5}
        gapSize={0.5}
      />
    </mesh>
  );
};

export default function MapBuilder3D() {
  const [nodes, setNodes] = useState<Node3D[]>([]);
  const [edges, setEdges] = useState<Edge3D[]>([]);
  const [mode, setMode] = useState<
    "add_node" | "add_waypoint" | "add_edge" | "edit_room" | "edit_building"
  >("add_node");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [selectedBuilding, setSelectedBuilding] = useState<
    "building_1" | "building_2" | null
  >(null);

  const [buildings, setBuildings] = useState<any>({
    building_1: {
      position: [-6, 0, 0],
      size: [10, 10],
      color: "#ffffff",
      name: "Building 1",
    },
    building_2: {
      position: [6, 0, -2],
      size: [10, 10],
      color: "#a5f3fc",
      name: "Building 2",
    },
  });

  const [draggingNode, setDraggingNode] = useState<string | null>(null);
  const [draggingBuilding, setDraggingBuilding] = useState<
    "building_1" | "building_2" | null
  >(null);
  const [orbitEnabled, setOrbitEnabled] = useState(true);

  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [currentFloor, setCurrentFloor] = useState<string>("floor_1");

  useEffect(() => {
    fetch(`/api/map?floor=${currentFloor}`)
      .then((res) => res.json())
      .then((data) => {
        if (data.nodes) setNodes(data.nodes);
        if (data.edges) setEdges(data.edges);
        if (data.buildings) setBuildings(data.buildings);
      })
      .catch(console.error);
  }, [currentFloor]);

  const handleSave = async () => {
    try {
      await fetch(`/api/map?floor=${currentFloor}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes, edges, buildings, format: "3d" }),
      });
      alert(`Map Saved Successfully to ${currentFloor}!`);
    } catch (e) {
      alert("Error saving map.");
    }
  };

  // --- DRAG SYSTEM LOGIC ---
  const handlePointerDownBuilding = (
    e: any,
    bId: "building_1" | "building_2",
  ) => {
    if (mode === "edit_building") {
      e.stopPropagation();
      setDraggingBuilding(bId);
      setSelectedBuilding(bId);
      setSelectedNode(null);
      setSelectedEdge(null);
      setOrbitEnabled(false);
    }
  };

  const handlePointerDownNode = (e: any, nodeId: string) => {
    if (mode === "edit_room") {
      e.stopPropagation();
      setDraggingNode(nodeId);
      setSelectedNode(nodeId);
      setSelectedEdge(null);
      setSelectedBuilding(null);
      setOrbitEnabled(false);
    }
  };

  const handleGlobalPointerMove = (e: any) => {
    if (draggingNode) {
      e.stopPropagation();
      const bId = e.point.x < 0 ? "building_1" : "building_2";
      const b = buildings[bId];
      const localX = Math.round(e.point.x - b.position[0]);
      const localZ = Math.round(e.point.z - b.position[2]);

      setNodes((prev) =>
        prev.map((n) =>
          n.id === draggingNode
            ? { ...n, x: localX, z: localZ, building: bId }
            : n,
        ),
      );
    } else if (draggingBuilding) {
      e.stopPropagation();
      const x = Math.round(e.point.x);
      const z = Math.round(e.point.z);
      setBuildings((prev: any) => ({
        ...prev,
        [draggingBuilding]: { ...prev[draggingBuilding], position: [x, 0, z] },
      }));
    }
  };

  const handleGlobalPointerUp = () => {
    setDraggingNode(null);
    setDraggingBuilding(null);
    setOrbitEnabled(true);
  };

  const handleGridClick = (e: any, buildingId: "building_1" | "building_2") => {
    if (draggingNode || draggingBuilding) return;
    e.stopPropagation();

    if (mode === "edit_building") {
      setSelectedBuilding(buildingId);
      setSelectedNode(null);
      setSelectedEdge(null);
      return;
    }

    if (mode === "add_node" || mode === "add_waypoint") {
      const x = Math.round(e.point.x - buildings[buildingId].position[0]);
      const z = Math.round(e.point.z - buildings[buildingId].position[2]);

      if (
        nodes.some((n) => n.x === x && n.z === z && n.building === buildingId)
      )
        return;

      if (mode === "add_node") {
        const label = prompt("Enter Room Name (e.g. Dean Office)");
        if (label) {
          setNodes([
            ...nodes,
            {
              id: Date.now().toString(),
              x,
              z,
              building: buildingId,
              label,
              type: "room",
              size: [1, 1, 1],
            },
          ]);
        }
      } else {
        setNodes([
          ...nodes,
          {
            id: Date.now().toString(),
            x,
            z,
            building: buildingId,
            label: "Waypoint",
            type: "waypoint",
          },
        ]);
      }
    }
  };

  const handleNodeClick = (e: any, nodeId: string) => {
    if (draggingNode) return;
    e.stopPropagation();

    if (mode === "add_edge") {
      if (!selectedNode) {
        setSelectedNode(nodeId);
      } else {
        if (selectedNode !== nodeId) {
          setEdges([
            ...edges,
            { id: Date.now().toString(), source: selectedNode, target: nodeId },
          ]);
        }
        setSelectedNode(null);
      }
    } else if (mode === "edit_room") {
      setSelectedNode(nodeId);
      setSelectedEdge(null);
    }
  };

  const handleNodeDoubleClick = (e: any, nodeId: string) => {
    e.stopPropagation();
    if (mode === "edit_room") {
      setNodes((prev) => prev.filter((n) => n.id !== nodeId));
      setEdges((prev) =>
        prev.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
      );
      setSelectedNode(null);
    }
  };

  const handleEdgeClick = (e: any, edgeId: string) => {
    e.stopPropagation();
    if (mode === "edit_room") {
      setSelectedEdge(edgeId);
      setSelectedNode(null);
      setSelectedBuilding(null);
    }
  };

  const handleEdgeDoubleClick = (e: any, edgeId: string) => {
    e.stopPropagation();
    if (mode === "edit_room") {
      setEdges((prev) => prev.filter((edge) => edge.id !== edgeId));
      setSelectedEdge(null);
    }
  };

  // Keyboard Delete Support
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Delete" || e.key === "Backspace") {
        if (document.activeElement?.tagName === "INPUT") return;

        if (selectedEdge) {
          setEdges((prev) => prev.filter((edge) => edge.id !== selectedEdge));
          setSelectedEdge(null);
        } else if (selectedNode) {
          setNodes((prev) => prev.filter((n) => n.id !== selectedNode));
          setEdges((prev) =>
            prev.filter(
              (edge) =>
                edge.source !== selectedNode && edge.target !== selectedNode,
            ),
          );
          setSelectedNode(null);
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedEdge, selectedNode]);

  const getNodePosition = (node: Node3D): [number, number, number] => {
    const b = buildings[node.building];
    if (node.type === "waypoint") {
      return [b.position[0] + node.x, 0.1, b.position[2] + node.z];
    }
    const yOffset = node.size ? node.size[1] / 2 : 0.5;
    return [b.position[0] + node.x, yOffset, b.position[2] + node.z];
  };

  const isDragging = draggingNode || draggingBuilding;

  return (
    <div className="flex flex-col h-screen bg-[#1e2024] text-white p-6 font-sans">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-2xl font-bold">3D Campus Builder</h1>
          <select
            className="bg-gray-800 border border-gray-600 rounded px-3 py-1 font-semibold outline-none focus:border-indigo-500"
            value={currentFloor}
            onChange={(e) => setCurrentFloor(e.target.value)}
          >
            <option value="floor_1">Floor 1</option>
            <option value="floor_2">Floor 2</option>
            <option value="floor_3">Floor 3</option>
            <option value="basement">Basement</option>
            <option value="outdoor">Outdoor Campus</option>
          </select>
        </div>
        <div className="flex gap-4">
          <button
            className={`px-4 py-2 rounded font-semibold ${mode === "add_node" ? "bg-indigo-600" : "bg-gray-700"}`}
            onClick={() => {
              setMode("add_node");
              setSelectedBuilding(null);
              setSelectedNode(null);
              setSelectedEdge(null);
            }}
          >
            + Place Room
          </button>
          <button
            className={`px-4 py-2 rounded font-semibold ${mode === "add_waypoint" ? "bg-teal-600" : "bg-gray-700"}`}
            onClick={() => {
              setMode("add_waypoint");
              setSelectedBuilding(null);
              setSelectedNode(null);
              setSelectedEdge(null);
            }}
          >
            📍 Place Waypoint
          </button>
          <button
            className={`px-4 py-2 rounded font-semibold ${mode === "add_edge" ? "bg-indigo-600" : "bg-gray-700"}`}
            onClick={() => {
              setMode("add_edge");
              setSelectedNode(null);
              setSelectedBuilding(null);
              setSelectedEdge(null);
            }}
          >
            ↗ Connect Path
          </button>
          <button
            className={`px-4 py-2 rounded font-semibold ${mode === "edit_room" ? "bg-rose-600" : "bg-gray-700"}`}
            onClick={() => {
              setMode("edit_room");
              setSelectedBuilding(null);
            }}
          >
            📦 Edit Items
          </button>
          <button
            className={`px-4 py-2 rounded font-semibold ${mode === "edit_building" ? "bg-amber-600" : "bg-gray-700"}`}
            onClick={() => {
              setMode("edit_building");
              setSelectedNode(null);
              setSelectedEdge(null);
            }}
          >
            🏗️ Edit Buildings
          </button>
          <button
            onClick={handleSave}
            className="px-6 py-2 rounded font-bold bg-green-500 hover:bg-green-600 text-white ml-8"
          >
            💾 Save {currentFloor.replace("_", " ").toUpperCase()}
          </button>
        </div>
      </div>

      <div className="flex gap-4 flex-1 min-h-0">
        {/* Main Canvas Area */}
        <div className="flex-1 rounded-xl overflow-hidden shadow-2xl relative border border-gray-700 cursor-crosshair">
          <Canvas
            shadows
            orthographic
            camera={{ position: [20, 20, 20], zoom: 40 }}
          >
            <ambientLight intensity={0.5} />
            <directionalLight
              position={[10, 20, 10]}
              intensity={1}
              castShadow
            />

            {/* Invisible Global Drag Plane - Only active during a drag */}
            {isDragging && (
              <mesh
                rotation={[-Math.PI / 2, 0, 0]}
                position={[0, 0.51, 0]}
                onPointerMove={handleGlobalPointerMove}
                onPointerUp={handleGlobalPointerUp}
                onPointerOut={handleGlobalPointerUp}
              >
                <planeGeometry args={[200, 200]} />
                <meshBasicMaterial transparent opacity={0} depthWrite={false} />
              </mesh>
            )}

            {/* Render Buildings */}
            {(Object.keys(buildings) as Array<"building_1" | "building_2">).map(
              (bId) => {
                const b = buildings[bId];
                const isSelected =
                  selectedBuilding === bId && mode === "edit_building";
                const isDraggingThis = draggingBuilding === bId;
                return (
                  <group key={bId} position={b.position}>
                    <mesh
                      rotation={[-Math.PI / 2, 0, 0]}
                      receiveShadow
                      onClick={(e) => handleGridClick(e, bId)}
                      onPointerDown={(e) => handlePointerDownBuilding(e, bId)}
                    >
                      <planeGeometry args={[b.size[0], b.size[1]]} />
                      <meshStandardMaterial
                        color={b.color}
                        transparent
                        opacity={isSelected ? 0.9 : 0.8}
                      />
                      {isSelected && (
                        <meshBasicMaterial
                          color={isDraggingThis ? "#22c55e" : "#fbbf24"}
                          wireframe
                        />
                      )}
                    </mesh>
                    <Grid
                      args={[b.size[0], b.size[1]]}
                      position={[0, 0.01, 0]}
                      cellSize={1}
                      cellThickness={1}
                      cellColor="#666"
                      sectionSize={1}
                      sectionThickness={isSelected ? 2 : 1.5}
                      sectionColor={
                        isDraggingThis
                          ? "#22c55e"
                          : isSelected
                            ? "#fbbf24"
                            : "#333"
                      }
                      fadeDistance={40}
                    />
                    <Text
                      position={[0, 0.02, b.size[1] / 2 + 0.5]}
                      rotation={[-Math.PI / 2, 0, 0]}
                      fontSize={0.8}
                      color={b.color}
                    >
                      {b.name}
                    </Text>
                  </group>
                );
              },
            )}

            {/* Render Nodes (Rooms & Waypoints) */}
            {nodes.map((node) => {
              const pos = getNodePosition(node);
              const isSelected =
                selectedNode === node.id &&
                (mode === "edit_room" || mode === "add_edge");
              const isDraggingThis = draggingNode === node.id;

              if (node.type === "waypoint") {
                return (
                  <group key={node.id} position={pos}>
                    <mesh
                      rotation={[-Math.PI / 2, 0, 0]}
                      onClick={(e) => handleNodeClick(e, node.id)}
                      onDoubleClick={(e) => handleNodeDoubleClick(e, node.id)}
                      onPointerDown={(e) => handlePointerDownNode(e, node.id)}
                    >
                      <circleGeometry args={[0.3, 32]} />
                      <meshStandardMaterial
                        color={
                          isDraggingThis
                            ? "#22c55e"
                            : isSelected
                              ? "#ef4444"
                              : "#14b8a6"
                        }
                        transparent
                        opacity={0.9}
                      />
                      {isSelected && mode === "edit_room" && (
                        <meshBasicMaterial
                          color={isDraggingThis ? "#22c55e" : "#ef4444"}
                          wireframe
                        />
                      )}
                    </mesh>
                  </group>
                );
              }

              const size = node.size || [BLOCK_SIZE, BLOCK_SIZE, BLOCK_SIZE];
              return (
                <group key={node.id} position={pos}>
                  <Box
                    args={size}
                    castShadow
                    onClick={(e) => handleNodeClick(e, node.id)}
                    onDoubleClick={(e) => handleNodeDoubleClick(e, node.id)}
                    onPointerDown={(e) => handlePointerDownNode(e, node.id)}
                  >
                    <meshStandardMaterial
                      color={
                        isDraggingThis
                          ? "#22c55e"
                          : isSelected
                            ? "#ef4444"
                            : "#334155"
                      }
                      transparent
                      opacity={isDraggingThis ? 0.8 : 1}
                    />
                    {isSelected && mode === "edit_room" && (
                      <meshBasicMaterial
                        color={isDraggingThis ? "#22c55e" : "#ef4444"}
                        wireframe
                      />
                    )}
                  </Box>
                  <Text
                    position={[0, size[1] / 2 + 0.5, 0]}
                    fontSize={0.4}
                    color="#ffffff"
                    anchorX="center"
                    anchorY="middle"
                  >
                    {node.label}
                  </Text>
                </group>
              );
            })}

            {/* Render Edges (Paths) */}
            {edges.map((edge) => {
              const sourceNode = nodes.find((n) => n.id === edge.source);
              const targetNode = nodes.find((n) => n.id === edge.target);
              if (!sourceNode || !targetNode) return null;

              const p1 = getNodePosition(sourceNode);
              const p2 = getNodePosition(targetNode);

              // Direct straight line between the points, even across buildings
              const points: [number, number, number][] = [p1, p2];

              return (
                <AnimatedPath
                  key={edge.id}
                  points={points}
                  onClick={(e: any) => handleEdgeClick(e, edge.id)}
                  onDoubleClick={(e: any) => handleEdgeDoubleClick(e, edge.id)}
                  isSelected={selectedEdge === edge.id && mode === "edit_room"}
                />
              );
            })}

            <OrbitControls
              enableZoom={true}
              enablePan={true}
              maxPolarAngle={Math.PI / 2 - 0.1}
              enabled={orbitEnabled}
            />
          </Canvas>
          <div className="absolute bottom-4 left-4 bg-black/60 p-4 rounded-lg text-sm pointer-events-none">
            <p className="text-indigo-300">
              Mode: <b>{mode.replace("_", " ").toUpperCase()}</b>
            </p>
          </div>
        </div>

        {/* Sidebar for Editing Buildings */}
        {mode === "edit_building" && (
          <div className="w-[300px] bg-gray-800 rounded-xl p-5 border border-gray-700 shadow-2xl flex flex-col gap-4">
            <h2 className="text-xl font-bold text-amber-500">Edit Building</h2>
            {!selectedBuilding ? (
              <p className="text-sm text-gray-400">
                Click on a grid to select a building to edit.
              </p>
            ) : (
              <div className="space-y-4">
                <div>
                  <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                    Building Name
                  </label>
                  <input
                    type="text"
                    value={buildings[selectedBuilding].name}
                    onChange={(e) =>
                      setBuildings({
                        ...buildings,
                        [selectedBuilding]: {
                          ...buildings[selectedBuilding],
                          name: e.target.value,
                        },
                      })
                    }
                    className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm mt-1"
                  />
                </div>

                <div className="space-y-2">
                  <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                    Position (X, Z)
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={buildings[selectedBuilding].position[0]}
                      onChange={(e) =>
                        setBuildings({
                          ...buildings,
                          [selectedBuilding]: {
                            ...buildings[selectedBuilding],
                            position: [
                              Number(e.target.value),
                              0,
                              buildings[selectedBuilding].position[2],
                            ],
                          },
                        })
                      }
                      className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                    />
                    <input
                      type="number"
                      value={buildings[selectedBuilding].position[2]}
                      onChange={(e) =>
                        setBuildings({
                          ...buildings,
                          [selectedBuilding]: {
                            ...buildings[selectedBuilding],
                            position: [
                              buildings[selectedBuilding].position[0],
                              0,
                              Number(e.target.value),
                            ],
                          },
                        })
                      }
                      className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                    Size (Width, Depth)
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={buildings[selectedBuilding].size[0]}
                      onChange={(e) =>
                        setBuildings({
                          ...buildings,
                          [selectedBuilding]: {
                            ...buildings[selectedBuilding],
                            size: [
                              Number(e.target.value),
                              buildings[selectedBuilding].size[1],
                            ],
                          },
                        })
                      }
                      className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                    />
                    <input
                      type="number"
                      value={buildings[selectedBuilding].size[1]}
                      onChange={(e) =>
                        setBuildings({
                          ...buildings,
                          [selectedBuilding]: {
                            ...buildings[selectedBuilding],
                            size: [
                              buildings[selectedBuilding].size[0],
                              Number(e.target.value),
                            ],
                          },
                        })
                      }
                      className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                    />
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Sidebar for Editing Items */}
        {mode === "edit_room" && (
          <div className="w-[300px] bg-gray-800 rounded-xl p-5 border border-gray-700 shadow-2xl flex flex-col gap-4">
            <h2 className="text-xl font-bold text-rose-500">Edit Item</h2>
            {!selectedNode && !selectedEdge ? (
              <p className="text-sm text-gray-400">
                Click on a room, waypoint, or path to edit it.
              </p>
            ) : selectedEdge ? (
              <div className="space-y-4">
                <p className="text-sm text-gray-300">
                  You have selected a Path.
                </p>
                <button
                  className="w-full mt-4 bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded"
                  onClick={() => {
                    if (confirm("Delete this path?")) {
                      setEdges(edges.filter((e) => e.id !== selectedEdge));
                      setSelectedEdge(null);
                    }
                  }}
                >
                  🗑 Delete Path
                </button>
              </div>
            ) : selectedNode ? (
              <div className="space-y-4">
                {(() => {
                  const node = nodes.find((n) => n.id === selectedNode);
                  if (!node) return null;
                  const size = node.size || [1, 1, 1];
                  const isWaypoint = node.type === "waypoint";

                  return (
                    <>
                      <div>
                        <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                          {isWaypoint ? "Waypoint Name" : "Room Name"}
                        </label>
                        <input
                          type="text"
                          value={node.label}
                          onChange={(e) =>
                            setNodes(
                              nodes.map((n) =>
                                n.id === node.id
                                  ? { ...n, label: e.target.value }
                                  : n,
                              ),
                            )
                          }
                          className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm mt-1"
                        />
                      </div>

                      <div className="space-y-2">
                        <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                          Position (X, Z)
                        </label>
                        <div className="flex gap-2">
                          <input
                            type="number"
                            step="0.5"
                            value={node.x}
                            onChange={(e) =>
                              setNodes(
                                nodes.map((n) =>
                                  n.id === node.id
                                    ? { ...n, x: Number(e.target.value) }
                                    : n,
                                ),
                              )
                            }
                            className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                          />
                          <input
                            type="number"
                            step="0.5"
                            value={node.z}
                            onChange={(e) =>
                              setNodes(
                                nodes.map((n) =>
                                  n.id === node.id
                                    ? { ...n, z: Number(e.target.value) }
                                    : n,
                                ),
                              )
                            }
                            className="w-1/2 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                          />
                        </div>
                      </div>

                      {!isWaypoint && (
                        <div className="space-y-2">
                          <label className="text-xs text-gray-400 uppercase font-bold tracking-wider">
                            Size (Width, Height, Depth)
                          </label>
                          <div className="flex gap-2">
                            <input
                              type="number"
                              step="0.5"
                              value={size[0]}
                              onChange={(e) =>
                                setNodes(
                                  nodes.map((n) =>
                                    n.id === node.id
                                      ? {
                                          ...n,
                                          size: [
                                            Number(e.target.value),
                                            size[1],
                                            size[2],
                                          ],
                                        }
                                      : n,
                                  ),
                                )
                              }
                              className="w-1/3 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                            />
                            <input
                              type="number"
                              step="0.5"
                              value={size[1]}
                              onChange={(e) =>
                                setNodes(
                                  nodes.map((n) =>
                                    n.id === node.id
                                      ? {
                                          ...n,
                                          size: [
                                            size[0],
                                            Number(e.target.value),
                                            size[2],
                                          ],
                                        }
                                      : n,
                                  ),
                                )
                              }
                              className="w-1/3 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                            />
                            <input
                              type="number"
                              step="0.5"
                              value={size[2]}
                              onChange={(e) =>
                                setNodes(
                                  nodes.map((n) =>
                                    n.id === node.id
                                      ? {
                                          ...n,
                                          size: [
                                            size[0],
                                            size[1],
                                            Number(e.target.value),
                                          ],
                                        }
                                      : n,
                                  ),
                                )
                              }
                              className="w-1/3 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm"
                            />
                          </div>
                        </div>
                      )}

                      <button
                        className="w-full mt-4 bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded"
                        onClick={() => {
                          if (
                            confirm(
                              `Delete this ${isWaypoint ? "waypoint" : "room"} entirely?`,
                            )
                          ) {
                            setNodes(nodes.filter((n) => n.id !== node.id));
                            setEdges(
                              edges.filter(
                                (e) =>
                                  e.source !== node.id && e.target !== node.id,
                              ),
                            );
                            setSelectedNode(null);
                          }
                        }}
                      >
                        🗑 Delete {isWaypoint ? "Waypoint" : "Room"}
                      </button>
                    </>
                  );
                })()}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
