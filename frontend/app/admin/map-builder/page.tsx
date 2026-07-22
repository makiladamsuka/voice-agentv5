'use client';

import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { Save, Trash2, RotateCcw, CheckCircle, AlertCircle, Layers, MapPin, Link, Edit, ZoomIn, ZoomOut, Maximize2, Undo2, Redo2 } from 'lucide-react';

// ─────────────────────────────────────────────────────────────────────
// TYPES
// ─────────────────────────────────────────────────────────────────────
interface MapNode {
  id: string;
  x: number;
  z: number;
  building: string;
  label: string;
  type?: 'room' | 'waypoint';
  size?: number[];
  color?: string;
}

interface MapEdge {
  id: string;
  source: string;
  target: string;
  visible?: boolean;
}

interface Building {
  position: [number, number, number];
  size: [number, number];
  name: string;
  color?: string;
  removed_cells?: string[];
}

interface FloorData {
  nodes: MapNode[];
  edges: MapEdge[];
  buildings: Record<string, Building>;
  format?: string;
}

type Mode = 'select' | 'add_waypoint' | 'connect' | 'delete' | 'move' | 'shape_building';

const COLORS = {
  bg: '#0F172A',
  card: '#1E293B',
  border: '#334155',
  text: '#F1F5F9',
  muted: '#94A3B8',
  room: '#1E3A5F',
  roomStroke: '#3B82F6',
  waypoint: '#14B8A6',
  edge: '#475569',
  edgeHover: '#F59E0B',
  selected: '#F59E0B',
  start: '#22C55E',
  end: '#EF4444',
  path: '#3B82F6',
  grid: '#1E293B',
};

export default function PathEditorPage() {
  // ── Floor & Data ──────────────────────────────────────────────────
  const [floor, setFloor] = useState('floor_1');
  const [floorData, setFloorData] = useState<FloorData | null>(null);
  const [nodes, setNodes] = useState<MapNode[]>([]);
  const [edges, setEdges] = useState<MapEdge[]>([]);
  const [buildings, setBuildings] = useState<Record<string, Building>>({});
  const [loading, setLoading] = useState(false);
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // ── Editor Mode ───────────────────────────────────────────────────
  const [mode, setMode] = useState<Mode>('select');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedBuildingId, setSelectedBuildingId] = useState<string | null>(null);
  // For connect mode: first node clicked
  const [connectFrom, setConnectFrom] = useState<string | null>(null);

  // ── History ───────────────────────────────────────────────────────
  const [hist, setHist] = useState<{ stack: FloorData[], index: number }>({ stack: [], index: -1 });
  const skipHistoryRef = useRef(false);

  useEffect(() => {
    if (skipHistoryRef.current) {
      skipHistoryRef.current = false;
      return;
    }
    if (!floorData) return;
    const timer = setTimeout(() => {
      setHist(prev => {
        const nextState = { ...floorData, nodes, edges, buildings };
        const lastState = prev.stack[prev.index];
        if (lastState && JSON.stringify({ n: lastState.nodes, e: lastState.edges, b: lastState.buildings }) === JSON.stringify({ n: nextState.nodes, e: nextState.edges, b: nextState.buildings })) {
          return prev;
        }
        const newStack = prev.stack.slice(0, prev.index + 1);
        newStack.push(JSON.parse(JSON.stringify(nextState)));
        if (newStack.length > 50) newStack.shift();
        return { stack: newStack, index: newStack.length - 1 };
      });
    }, 500);
    return () => clearTimeout(timer);
  }, [nodes, edges, buildings, floorData]);

  const handleUndo = () => {
    setHist(prev => {
      if (prev.index > 0) {
        skipHistoryRef.current = true;
        const state = prev.stack[prev.index - 1];
        setNodes(state.nodes);
        setEdges(state.edges);
        setBuildings(state.buildings);
        return { ...prev, index: prev.index - 1 };
      }
      return prev;
    });
  };

  const handleRedo = () => {
    setHist(prev => {
      if (prev.index < prev.stack.length - 1) {
        skipHistoryRef.current = true;
        const state = prev.stack[prev.index + 1];
        setNodes(state.nodes);
        setEdges(state.edges);
        setBuildings(state.buildings);
        return { ...prev, index: prev.index + 1 };
      }
      return prev;
    });
  };

  // ── SVG Pan/Zoom ─────────────────────────────────────────────────
  const [zoom, setZoom] = useState(1.2);
  const [pan, setPan] = useState({ x: 80, y: 60 });
  const [isDrag, setIsDrag] = useState(false);
  const dragRef = useRef({ x: 0, y: 0 });
  const svgRef = useRef<SVGSVGElement>(null);
  const SVG_W = 1100, SVG_H = 700, PAD = 60;

  // ── Load floor data ───────────────────────────────────────────────
  const loadFloor = useCallback(() => {
    setLoading(true);
    fetch(`/api/map?floor=${floor}`)
      .then(r => r.json())
      .then((d: FloorData) => {
        setFloorData(d);
        setNodes(d.nodes || []);
        setEdges(d.edges || []);
        setBuildings(d.buildings || {});
        setLoading(false);
        setHist({ stack: [JSON.parse(JSON.stringify({ ...d, nodes: d.nodes || [], edges: d.edges || [], buildings: d.buildings || {} }))], index: 0 });
        skipHistoryRef.current = true;
      })
      .catch(() => setLoading(false));
  }, [floor]);

  useEffect(() => {
    loadFloor();
    setMode('select');
    setSelectedId(null);
    setSelectedBuildingId(null);
    setConnectFrom(null);
  }, [loadFloor]);

  // ── Coordinate helpers ────────────────────────────────────────────
  const bbox = useMemo(() => {
    const bVals = Object.values(buildings);
    if (!bVals.length) return { minX: -15, maxX: 25, minZ: -12, maxZ: 20 };
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    bVals.forEach(b => {
      const hw = b.size[0] / 2, hh = b.size[1] / 2;
      minX = Math.min(minX, b.position[0] - hw); maxX = Math.max(maxX, b.position[0] + hw);
      minZ = Math.min(minZ, b.position[2] - hh); maxZ = Math.max(maxZ, b.position[2] + hh);
    });
    return { minX: minX - 3, maxX: maxX + 3, minZ: minZ - 3, maxZ: maxZ + 3 };
  }, [buildings]);

  const scale = useMemo(
    () => (SVG_W - 2 * PAD) / (bbox.maxX - bbox.minX),
    [bbox]
  );

  const toSVG = useCallback((wx: number, wz: number): [number, number] => [
    PAD + (wx - bbox.minX) * scale,
    PAD + (wz - bbox.minZ) * scale,
  ], [bbox, scale]);

  const toWorld = useCallback((sx: number, sy: number): [number, number] => [
    bbox.minX + (sx - PAD) / scale,
    bbox.minZ + (sy - PAD) / scale,
  ], [bbox, scale]);

  // World position of a node
  const nodeWorldPos = useCallback((n: MapNode): [number, number] => {
    const b = buildings[n.building] || { position: [0, 0, 0] };
    return [b.position[0] + n.x, b.position[2] + n.z];
  }, [buildings]);

  // ── Save to server ────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!floorData) return;
    const payload: FloorData = {
      ...floorData,
      nodes,
      edges,
      buildings,
    };
    try {
      await fetch(`/api/map?floor=${floor}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setSaveMsg({ ok: true, text: `Floor ${floor.replace('floor_', '')} saved! ${nodes.filter(n => n.type === 'waypoint').length} waypoints, ${edges.length} edges.` });
      setTimeout(() => setSaveMsg(null), 4000);
    } catch {
      setSaveMsg({ ok: false, text: 'Save failed. Check server.' });
      setTimeout(() => setSaveMsg(null), 4000);
    }
  }, [floorData, nodes, edges, buildings, floor]);

  // ── Clear all navigation (keep rooms) ────────────────────────────
  const handleClearNav = () => {
    if (!confirm(`Remove ALL waypoints and edges from ${floor.replace('floor_', 'Floor ')}? Rooms stay. You cannot undo unless you reload.`)) return;
    setNodes(prev => prev.filter(n => n.type !== 'waypoint'));
    setEdges([]);
    setSelectedId(null);
    setConnectFrom(null);
  };

  // ── SVG click → world coordinate ─────────────────────────────────
  const getSVGPoint = (e: React.MouseEvent): [number, number] | null => {
    const svg = svgRef.current;
    if (!svg) return null;
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const t = pt.matrixTransform(ctm.inverse());
    // Undo the pan/zoom transform
    return [(t.x - pan.x) / zoom, (t.y - pan.y) / zoom];
  };

  // ── Handle SVG canvas click (not hitting a node) ──────────────────
  const handleCanvasClick = (e: React.MouseEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    if (isDrag) return;
    const svgPt = getSVGPoint(e);
    if (!svgPt) return;
    const [sx, sy] = svgPt;

    if (mode === 'add_waypoint' || mode === 'shape_building') {
      // Find which building was clicked
      let clickedBuilding: string | null = null;
      for (const [bid, b] of Object.entries(buildings)) {
        const hw = b.size[0] / 2, hh = b.size[1] / 2;
        const [bsx, bsy] = toSVG(b.position[0] - hw, b.position[2] - hh);
        const bw = b.size[0] * scale, bh = b.size[1] * scale;
        if (sx >= bsx && sx <= bsx + bw && sy >= bsy && sy <= bsy + bh) {
          clickedBuilding = bid;
          break;
        }
      }
      if (!clickedBuilding) return;

      if (mode === 'shape_building') {
        setSelectedBuildingId(clickedBuilding);
        const b = buildings[clickedBuilding];
        const hw = b.size[0] / 2;
        const hh = b.size[1] / 2;
        const [wx, wz] = toWorld(sx, sy);
        const lx = wx - (b.position[0] - hw);
        const lz = wz - (b.position[2] - hh);
        const col = Math.floor(lx);
        const row = Math.floor(lz);
        if (col >= 0 && col < b.size[0] && row >= 0 && row < b.size[1]) {
          const cellId = `${col}_${row}`;
          setBuildings(prev => {
            const bPrev = prev[clickedBuilding!];
            const cells = bPrev.removed_cells || [];
            const newCells = cells.includes(cellId) ? cells.filter(c => c !== cellId) : [...cells, cellId];
            return { ...prev, [clickedBuilding!]: { ...bPrev, removed_cells: newCells } };
          });
        }
        return;
      }

      const b = buildings[clickedBuilding];
      // Convert SVG pt back to world, then to local building coords
      const [wx, wz] = toWorld(sx, sy);
      const localX = Math.round((wx - b.position[0]) * 2) / 2;
      const localZ = Math.round((wz - b.position[2]) * 2) / 2;
      const newNode: MapNode = {
        id: Date.now().toString(),
        x: localX,
        z: localZ,
        building: clickedBuilding,
        label: 'Waypoint',
        type: 'waypoint',
      };
      setNodes(prev => [...prev, newNode]);
    }

    // Deselect if clicking empty space in select mode
    if (mode === 'select') {
      setSelectedId(null);
    }
    if (mode === 'connect') {
      setConnectFrom(null);
    }
  };

  // ── Handle node click ─────────────────────────────────────────────
  const handleNodeClick = (e: React.MouseEvent, nodeId: string) => {
    e.stopPropagation();
    if (isDrag) return;

    if (mode === 'select') {
      setSelectedId(prev => prev === nodeId ? null : nodeId);
    }

    if (mode === 'connect') {
      if (!connectFrom) {
        setConnectFrom(nodeId);
      } else {
        if (connectFrom !== nodeId) {
          // Check duplicate
          const exists = edges.some(ed =>
            (ed.source === connectFrom && ed.target === nodeId) ||
            (ed.source === nodeId && ed.target === connectFrom)
          );
          if (!exists) {
            setEdges(prev => [...prev, {
              id: `e_${connectFrom}_${nodeId}_${Date.now()}`,
              source: connectFrom,
              target: nodeId,
              visible: true,
            }]);
          }
        }
        setConnectFrom(null);
      }
    }

    if (mode === 'delete') {
      // Delete node + its edges
      setNodes(prev => prev.filter(n => n.id !== nodeId));
      setEdges(prev => prev.filter(ed => ed.source !== nodeId && ed.target !== nodeId));
      setSelectedId(null);
    }
  };

  // ── Handle edge click ─────────────────────────────────────────────
  const handleEdgeClick = (e: React.MouseEvent, edgeId: string) => {
    e.stopPropagation();
    if (isDrag) return;
    if (mode === 'delete' || mode === 'select') {
      setEdges(prev => prev.filter(ed => ed.id !== edgeId));
    }
  };

  // ── Drag to move nodes and buildings ───────────────────────────────
  const dragNodeRef = useRef<string | null>(null);
  const dragNodeStart = useRef({ sx: 0, sy: 0, ox: 0, oz: 0 });

  const dragBuildingRef = useRef<string | null>(null);
  const dragBuildingStart = useRef({ sx: 0, sy: 0, ox: 0, oz: 0 });

  const handleNodeMouseDown = (e: React.MouseEvent, nodeId: string) => {
    if (mode !== 'select' && mode !== 'move') return;
    e.stopPropagation();
    const node = nodes.find(n => n.id === nodeId);
    if (!node) return;
    // In select mode only waypoints can be dragged; in move mode everything can
    if (mode === 'select' && node.type !== 'waypoint') return;
    const svgPt = getSVGPoint(e);
    if (!svgPt) return;
    dragNodeRef.current = nodeId;
    dragNodeStart.current = { sx: svgPt[0], sy: svgPt[1], ox: node.x, oz: node.z };
    setSelectedId(nodeId);
  };

  const handleBuildingMouseDown = (e: React.MouseEvent, bid: string) => {
    if (mode !== 'move') return;
    e.stopPropagation();
    const b = buildings[bid];
    if (!b) return;
    const svgPt = getSVGPoint(e);
    if (!svgPt) return;
    dragBuildingRef.current = bid;
    dragBuildingStart.current = { sx: svgPt[0], sy: svgPt[1], ox: b.position[0], oz: b.position[2] };
    setSelectedId(null);
  };

  // ── Pan & Zoom ────────────────────────────────────────────────────
  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    setZoom(z => Math.max(0.4, Math.min(6, e.deltaY < 0 ? z * 1.12 : z / 1.12)));
  };

  const onMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    if (dragNodeRef.current || dragBuildingRef.current) return;
    setIsDrag(false);
    dragRef.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
  };

  const onMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    // Node drag
    if (dragNodeRef.current) {
      const svgPt = getSVGPoint(e);
      if (!svgPt) return;
      const dx = (svgPt[0] - dragNodeStart.current.sx) / scale;
      const dz = (svgPt[1] - dragNodeStart.current.sy) / scale;
      const nx = Math.round((dragNodeStart.current.ox + dx) * 2) / 2;
      const nz = Math.round((dragNodeStart.current.oz + dz) * 2) / 2;
      setNodes(prev => prev.map(n => n.id === dragNodeRef.current ? { ...n, x: nx, z: nz } : n));
      return;
    }
    // Building drag
    if (dragBuildingRef.current) {
      const svgPt = getSVGPoint(e);
      if (!svgPt) return;
      const dx = (svgPt[0] - dragBuildingStart.current.sx) / scale;
      const dz = (svgPt[1] - dragBuildingStart.current.sy) / scale;
      const nx = Math.round((dragBuildingStart.current.ox + dx) * 2) / 2;
      const nz = Math.round((dragBuildingStart.current.oz + dz) * 2) / 2;
      setBuildings(prev => ({
        ...prev,
        [dragBuildingRef.current!]: {
          ...prev[dragBuildingRef.current!],
          position: [nx, prev[dragBuildingRef.current!].position[1], nz]
        }
      }));
      return;
    }
    // Canvas pan
    const dist = Math.sqrt(
      Math.pow(e.clientX - (dragRef.current.x + pan.x), 2) +
      Math.pow(e.clientY - (dragRef.current.y + pan.y), 2)
    );
    if (dist > 3) setIsDrag(true);
    if (e.buttons === 1 && !dragNodeRef.current) {
      setPan({ x: e.clientX - dragRef.current.x, y: e.clientY - dragRef.current.y });
    }
  };

  const onMouseUp = () => {
    dragNodeRef.current = null;
    dragBuildingRef.current = null;
    setTimeout(() => setIsDrag(false), 0);
  };

  // ── Computed ──────────────────────────────────────────────────────
  const rooms = useMemo(() => nodes.filter(n => n.type !== 'waypoint'), [nodes]);
  const waypoints = useMemo(() => nodes.filter(n => n.type === 'waypoint'), [nodes]);

  const cursorClass =
    mode === 'add_waypoint' ? 'cursor-crosshair' :
      mode === 'connect' ? 'cursor-cell' :
        mode === 'delete' ? 'cursor-not-allowed' :
          mode === 'move' ? 'cursor-move' :
            isDrag ? 'cursor-grabbing' : 'cursor-grab';

  // ─────────────────────────────────────────────────────────────────
  // RENDER
  // ─────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-screen overflow-hidden" style={{ background: COLORS.bg, color: COLORS.text, fontFamily: 'sans-serif' }}>

      {/* ── TOP TOOLBAR ── */}
      <div className="flex items-center gap-2 px-4 py-3 border-b shrink-0 flex-wrap"
        style={{ borderColor: COLORS.border, background: COLORS.card }}>

        {/* Floor selector */}
        <div className="flex items-center gap-2 mr-2">
          <Layers className="w-4 h-4" style={{ color: COLORS.muted }} />
          <select
            className="rounded-lg px-3 py-1.5 text-sm font-semibold outline-none border"
            style={{ background: COLORS.bg, borderColor: COLORS.border, color: COLORS.text }}
            value={floor}
            onChange={e => setFloor(e.target.value)}
          >
            <option value="floor_1">Floor 1</option>
            <option value="floor_2">Floor 2</option>
            <option value="floor_3">Floor 3</option>
            <option value="floor_4">Floor 4</option>
          </select>
        </div>

        <div className="w-px h-7 mx-1" style={{ background: COLORS.border }} />

        {/* Mode buttons */}
        {([
          { m: 'select', label: '✋ Select', active: '#F59E0B' },
          { m: 'move', label: '✥ Move Items', active: '#EC4899' },
          { m: 'shape_building', label: '🏗 Edit Shape', active: '#8B5CF6' },
          { m: 'add_waypoint', label: '📍 Add Waypoint', active: '#14B8A6' },
          { m: 'connect', label: '↗ Connect Path', active: '#6366F1' },
          { m: 'delete', label: '🗑 Delete', active: '#EF4444' },
        ] as const).map(({ m, label, active }) => (
          <button key={m}
            className="px-3 py-1.5 rounded-lg text-xs font-bold transition-all"
            style={{
              background: mode === m ? active : COLORS.border,
              color: mode === m ? '#fff' : COLORS.muted,
              outline: mode === m ? `2px solid ${active}` : 'none',
              outlineOffset: 1,
            }}
            onClick={() => { setMode(m as Mode); setSelectedId(null); setConnectFrom(null); }}
          >{label}</button>
        ))}

        <div className="w-px h-7 mx-1" style={{ background: COLORS.border }} />

        {/* Undo/Redo */}
        <button
          className="px-2 py-1.5 rounded-lg text-xs font-bold border transition-all hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent flex items-center"
          style={{ borderColor: COLORS.border, color: COLORS.text, background: COLORS.card }}
          onClick={handleUndo}
          disabled={hist.index <= 0}
          title="Undo"
        >
          <Undo2 className="w-4 h-4" />
        </button>
        <button
          className="px-2 py-1.5 rounded-lg text-xs font-bold border transition-all hover:bg-slate-800 disabled:opacity-30 disabled:hover:bg-transparent flex items-center"
          style={{ borderColor: COLORS.border, color: COLORS.text, background: COLORS.card }}
          onClick={handleRedo}
          disabled={hist.index >= hist.stack.length - 1}
          title="Redo"
        >
          <Redo2 className="w-4 h-4" />
        </button>

        <div className="w-px h-7 mx-1" style={{ background: COLORS.border }} />

        {/* Clear nav */}
        <button
          className="px-3 py-1.5 rounded-lg text-xs font-bold border transition-all hover:bg-orange-900"
          style={{ borderColor: '#92400E', color: '#FB923C', background: '#431407' }}
          onClick={handleClearNav}
          title="Remove all waypoints and edges. Rooms stay."
        >
          <Trash2 className="w-3 h-3 inline mr-1" />
          Clear All Paths
        </button>

        {/* Reload from file */}
        <button
          className="px-3 py-1.5 rounded-lg text-xs font-bold border transition-all hover:opacity-80"
          style={{ borderColor: COLORS.border, color: COLORS.muted, background: COLORS.bg }}
          onClick={loadFloor}
          title="Reload from saved file (discard unsaved changes)"
        >
          ↺ Reload
        </button>

        <div className="flex-1" />

        {/* Stats */}
        <span className="text-[11px]" style={{ color: COLORS.muted }}>
          {rooms.length} rooms · {waypoints.length} waypoints · {edges.length} edges
        </span>

        {/* Save */}
        <button
          className="px-5 py-1.5 rounded-lg text-sm font-bold flex items-center gap-2 transition-all hover:opacity-90"
          style={{ background: '#22C55E', color: '#fff' }}
          onClick={handleSave}
        >
          <Save className="w-4 h-4" />
          Save Map
        </button>
      </div>

      {/* Save status */}
      {saveMsg && (
        <div className="px-4 py-2 flex items-center gap-2 text-xs font-medium shrink-0"
          style={{ background: saveMsg.ok ? '#052E16' : '#450A0A', color: saveMsg.ok ? '#4ADE80' : '#F87171' }}>
          {saveMsg.ok ? <CheckCircle className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
          {saveMsg.text}
        </div>
      )}

      {/* Mode hint */}
      <div className="px-4 py-1.5 text-[11px] shrink-0 flex items-center gap-3"
        style={{ background: '#0F172A', color: COLORS.muted, borderBottom: `1px solid ${COLORS.border}` }}>
        {mode === 'select' && (
          <div className="flex items-center gap-4">
            <span>✋ Click an item to select it. Drag a waypoint to reposition. Click an edge to delete it.</span>
            {selectedId && nodes.find(n => n.id === selectedId)?.type !== 'waypoint' && (
              <div className="flex items-center gap-3 ml-4 border-l border-slate-700 pl-4">
                <span className="font-bold text-white">{nodes.find(n => n.id === selectedId)?.label} Size:</span>
                <label className="flex items-center gap-1">W:
                  <input type="number" min="0.5" step="0.5" max="20"
                    value={nodes.find(n => n.id === selectedId)?.size?.[0] || 1}
                    onChange={e => {
                      const v = Number(e.target.value) || 1;
                      setNodes(prev => prev.map(n => n.id === selectedId ? { ...n, size: [v, n.size?.[1] || 1, n.size?.[2] || 1] } : n));
                    }}
                    className="w-16 bg-slate-800 px-1 py-0.5 rounded outline-none border border-slate-600 text-white" />
                </label>
                <label className="flex items-center gap-1">D:
                  <input type="number" min="0.5" step="0.5" max="20"
                    value={nodes.find(n => n.id === selectedId)?.size?.[2] || 1}
                    onChange={e => {
                      const v = Number(e.target.value) || 1;
                      setNodes(prev => prev.map(n => n.id === selectedId ? { ...n, size: [n.size?.[0] || 1, n.size?.[1] || 1, v] } : n));
                    }}
                    className="w-16 bg-slate-800 px-1 py-0.5 rounded outline-none border border-slate-600 text-white" />
                </label>
              </div>
            )}
          </div>
        )}
        {mode === 'move' && '✥ Click and drag any room, waypoint, or building to move it. Release to place. Edges follow automatically.'}
        {mode === 'add_waypoint' && '📍 Click anywhere inside a building to place a new waypoint. Place them along corridors and at junctions.'}
        {mode === 'connect' && (connectFrom
          ? `↗ Now click a second room or waypoint to connect it to the selected node…`
          : '↗ Click the FIRST room or waypoint to start drawing a path edge.')}
        {mode === 'delete' && '🗑 Click any waypoint or edge to delete it. Rooms cannot be deleted.'}
        {mode === 'shape_building' && (
          <div className="flex items-center gap-4">
            <span>🏗 Click on a building to select it. Click grid cells to remove/add them.</span>
            {selectedBuildingId && buildings[selectedBuildingId] && (
              <div className="flex items-center gap-3">
                <span className="font-bold text-white">{buildings[selectedBuildingId].name}</span>
                <label className="flex items-center gap-1">Width:
                  <input type="number" min="1" max="50" value={buildings[selectedBuildingId].size[0]}
                    onChange={e => {
                      const v = Number(e.target.value) || 1;
                      setBuildings(p => ({ ...p, [selectedBuildingId]: { ...p[selectedBuildingId], size: [v, p[selectedBuildingId].size[1]] } }));
                    }}
                    className="w-16 bg-slate-800 px-1 py-0.5 rounded outline-none border border-slate-600 text-white" />
                </label>
                <label className="flex items-center gap-1">Depth:
                  <input type="number" min="1" max="50" value={buildings[selectedBuildingId].size[1]}
                    onChange={e => {
                      const v = Number(e.target.value) || 1;
                      setBuildings(p => ({ ...p, [selectedBuildingId]: { ...p[selectedBuildingId], size: [p[selectedBuildingId].size[0], v] } }));
                    }}
                    className="w-16 bg-slate-800 px-1 py-0.5 rounded outline-none border border-slate-600 text-white" />
                </label>
                <label className="flex items-center gap-1">🎨 Color:
                  <input
                    type="color"
                    value={buildings[selectedBuildingId].color || '#ffffff'}
                    onChange={e => {
                      setBuildings(p => ({ ...p, [selectedBuildingId]: { ...p[selectedBuildingId], color: e.target.value } }));
                    }}
                    className="w-9 h-8 rounded cursor-pointer border border-slate-600 bg-transparent p-0.5"
                    title="Change building color"
                  />
                </label>
              </div>
            )}
          </div>
        )}
        {connectFrom && (
          <span className="ml-auto font-bold" style={{ color: '#6366F1' }}>
            Connecting from: {nodes.find(n => n.id === connectFrom)?.label ?? connectFrom}
          </span>
        )}
      </div>

      {/* ── MAP SVG ── */}
      <div className="flex-1 overflow-hidden relative">
        {loading ? (
          <div className="w-full h-full flex items-center justify-center text-sm animate-pulse" style={{ color: COLORS.muted }}>
            Loading floor data…
          </div>
        ) : (
          <svg ref={svgRef}
            className={`w-full h-full select-none ${cursorClass}`}
            style={{ background: COLORS.bg }}
            viewBox={`0 0 ${SVG_W} ${SVG_H}`}
            preserveAspectRatio="xMidYMid meet"
            onWheel={onWheel}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
            onClick={handleCanvasClick}
          >
            <defs>
              <pattern id="dots" width={scale * 0.5} height={scale * 0.5} patternUnits="userSpaceOnUse"
                patternTransform={`translate(${pan.x % (scale * 0.5 * zoom)},${pan.y % (scale * 0.5 * zoom)}) scale(${zoom})`}>
                <circle cx={scale * 0.25} cy={scale * 0.25} r={0.8} fill="#1E293B" />
              </pattern>
            </defs>

            {/* Dot grid background */}
            <rect width={SVG_W} height={SVG_H} fill="url(#dots)" />

            <g transform={`translate(${pan.x},${pan.y}) scale(${zoom})`}>

              {/* ── Buildings ── */}
              {Object.entries(buildings).map(([bid, b]) => {
                const hw = b.size[0] / 2, hh = b.size[1] / 2;
                const [sx, sy] = toSVG(b.position[0] - hw, b.position[2] - hh);
                const bw = b.size[0] * scale, bh = b.size[1] * scale;
                return (
                  <g key={bid}
                    onMouseDown={e => handleBuildingMouseDown(e, bid)}
                    style={{ cursor: mode === 'move' ? 'move' : mode === 'shape_building' ? 'crosshair' : 'default' }}>
                    {/* Floor fill */}
                    <rect x={sx} y={sy} width={bw} height={bh} rx={4}
                      fill="#1E293B" stroke="#334155" strokeWidth={2} />
                    {/* Removed cells */}
                    {(b.removed_cells || []).map(cellId => {
                      const [cx, cz] = cellId.split('_').map(Number);
                      return <rect key={cellId} x={sx + cx * scale} y={sy + cz * scale} width={scale + 0.5} height={scale + 0.5} fill="#0F172A" />
                    })}
                    {/* Grid lines */}
                    <path
                      d={[
                        ...Array.from({ length: Math.round(b.size[0]) + 1 }, (_, i) => `M ${sx + i * scale} ${sy} v ${bh}`),
                        ...Array.from({ length: Math.round(b.size[1]) + 1 }, (_, i) => `M ${sx} ${sy + i * scale} h ${bw}`),
                      ].join(' ')}
                      fill="none" stroke="#0F172A" strokeWidth={0.5}
                    />
                    {/* Highlight selected building in shape mode */}
                    {mode === 'shape_building' && selectedBuildingId === bid && (
                      <rect x={sx} y={sy} width={bw} height={bh} fill="none" stroke="#8B5CF6" strokeWidth={3} style={{ pointerEvents: 'none' }} />
                    )}
                    {/* Building label */}
                    <text x={sx + 10} y={sy + 18} fontSize={11} fontWeight="bold"
                      fill="#475569" style={{ userSelect: 'none', pointerEvents: 'none' }}>
                      {b.name}
                    </text>
                  </g>
                );
              })}

              {/* ── Edges ── */}
              {edges.map(edge => {
                const src = nodes.find(n => n.id === edge.source);
                const tgt = nodes.find(n => n.id === edge.target);
                if (!src || !tgt) return null;
                const [x1, y1] = toSVG(...nodeWorldPos(src));
                const [x2, y2] = toSVG(...nodeWorldPos(tgt));
                return (
                  <line key={edge.id}
                    x1={x1} y1={y1} x2={x2} y2={y2}
                    stroke={COLORS.path} strokeWidth={10}
                    strokeDasharray="6 3"
                    strokeLinecap="round"
                    opacity={0.8}
                    className="cursor-pointer hover:stroke-yellow-400"
                    onClick={e => handleEdgeClick(e, edge.id)}
                    style={{ cursor: mode === 'delete' || mode === 'select' ? 'pointer' : 'default' }}
                  />
                );
              })}

              {/* ── Rooms ── */}
              {rooms.map(n => {
                const [cx, cy] = toSVG(...nodeWorldPos(n));
                const sz = n.size ?? [1, 1, 1];
                const rw = Math.max(sz[0] * scale, 32);
                const rh = Math.max(sz[2] * scale, 24);
                const rx = cx - rw / 2, ry = cy - rh / 2;
                const isSel = selectedId === n.id;
                const isConnFrom = connectFrom === n.id;
                const isMovable = mode === 'move';
                return (
                  <g key={n.id}
                    style={{ cursor: isMovable ? 'move' : 'pointer' }}
                    onClick={e => handleNodeClick(e, n.id)}
                    onMouseDown={e => handleNodeMouseDown(e, n.id)}>
                    <rect x={rx} y={ry} width={rw} height={rh} rx={4}
                      fill={n.color ?? COLORS.room}
                      stroke={isConnFrom ? '#A855F7' : isSel ? COLORS.selected : COLORS.roomStroke}
                      strokeWidth={isConnFrom || isSel ? 2.5 : 1.5}
                      strokeDasharray={isMovable ? '4 2' : 'none'}
                    />
                    {isMovable && (
                      <text x={cx} y={ry + 10} textAnchor="middle"
                        fontSize={8} fill="#EC4899" fontWeight="bold"
                        style={{ userSelect: 'none', pointerEvents: 'none' }}>✥</text>
                    )}
                    <text x={cx} y={cy} textAnchor="middle" dominantBaseline="central"
                      fontSize={Math.min(9, rw * 0.16)} fontWeight="600" fill="#93C5FD"
                      style={{ userSelect: 'none', pointerEvents: 'none' }}>
                      {n.label.length > 18 ? n.label.substring(0, 16) + '…' : n.label}
                    </text>
                  </g>
                );
              })}

              {/* ── Waypoints ── */}
              {waypoints.map(n => {
                const [cx, cy] = toSVG(...nodeWorldPos(n));
                const isSel = selectedId === n.id;
                const isConnFrom = connectFrom === n.id;
                return (
                  <g key={n.id}
                    style={{ cursor: mode === 'select' ? 'move' : 'pointer' }}
                    onClick={e => handleNodeClick(e, n.id)}
                    onMouseDown={e => handleNodeMouseDown(e, n.id)}>
                    <circle cx={cx} cy={cy} r={7}
                      fill={isConnFrom ? '#A855F7' : isSel ? COLORS.selected : COLORS.waypoint}
                      stroke="#fff" strokeWidth={1.5}
                    />
                    {/* Cross marker */}
                    <line x1={cx - 4} y1={cy} x2={cx + 4} y2={cy} stroke="#fff" strokeWidth={1.2} style={{ pointerEvents: 'none' }} />
                    <line x1={cx} y1={cy - 4} x2={cx} y2={cy + 4} stroke="#fff" strokeWidth={1.2} style={{ pointerEvents: 'none' }} />
                  </g>
                );
              })}

              {/* Highlight: "connect from" indicator ring */}
              {connectFrom && (() => {
                const n = nodes.find(x => x.id === connectFrom);
                if (!n) return null;
                const [cx, cy] = toSVG(...nodeWorldPos(n));
                return <circle cx={cx} cy={cy} r={14} fill="none" stroke="#A855F7" strokeWidth={2} strokeDasharray="4 2" />;
              })()}

            </g>
          </svg>
        )}

        {/* Zoom controls */}
        <div className="absolute bottom-4 right-4 flex flex-col gap-1">
          {[['＋', () => setZoom(z => Math.min(6, z * 1.2))],
          ['－', () => setZoom(z => Math.max(0.4, z / 1.2))],
          ['⊡', () => { setZoom(1.2); setPan({ x: 80, y: 60 }); }],
          ].map(([lbl, fn]: any) => (
            <button key={lbl}
              className="w-9 h-9 rounded-lg text-base font-bold flex items-center justify-center transition hover:opacity-80"
              style={{ background: COLORS.card, color: COLORS.muted, border: `1px solid ${COLORS.border}` }}
              onClick={fn}>{lbl}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
