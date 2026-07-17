"""
Indoor Navigation Engine - University Faculty Building
=======================================================
Multi-floor Dijkstra pathfinding with auto-heal for disconnected rooms.
Node IDs are scoped as floor_N::original_id to avoid collisions.
"""
import json, math, heapq, glob
from pathlib import Path
from difflib import SequenceMatcher
from collections import deque

FLOOR_CHANGE_COST = 50.0   # penalty to discourage unnecessary floor changes

class Wayfinder:
    ROBOT_LOCATION = "Front desk"

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = str(Path(__file__).resolve().parent.parent / "data")
        self.data_dir = Path(data_dir)
        self.floors: dict = {}
        self.nodes: dict = {}      # scoped_id -> node dict
        self.edges: list = []
        self.buildings: dict = {}  # scoped + plain -> building dict
        self.graph: dict = {}      # scoped_id -> [(neighbour_id, weight)]
        self.load_all_floors()

    # ── Loading ────────────────────────────────────────────────────────────

    def load_all_floors(self):
        files = sorted(glob.glob(str(self.data_dir / "map_graph_floor_*.json")))
        if not files:
            print("⚠️  Wayfinder: no map files found in", self.data_dir)
            return
        for fp in files:
            name = Path(fp).stem.replace("map_graph_", "")
            with open(fp, encoding="utf-8") as f:
                self.floors[name] = json.load(f)
            n = len(self.floors[name].get("nodes", []))
            e = len(self.floors[name].get("edges", []))
            print(f"  ✅ Loaded {name} ({n} nodes, {e} edges)")
        self._build_graph()

    def reload(self):
        self.floors.clear(); self.nodes.clear()
        self.edges.clear(); self.buildings.clear(); self.graph.clear()
        self.load_all_floors()

    # ── Graph build ────────────────────────────────────────────────────────

    def _build_graph(self):
        for floor_name, data in self.floors.items():
            buildings = data.get("buildings", {})
            for bid, bdata in buildings.items():
                scoped = f"{floor_name}::{bid}"
                self.buildings[scoped] = {**bdata, "floor": floor_name}
                self.buildings.setdefault(bid, bdata)

            for node in data.get("nodes", []):
                orig = node["id"]
                nid  = f"{floor_name}::{orig}"
                bld  = buildings.get(node["building"], {})
                bpos = bld.get("position", [0, 0, 0])
                self.nodes[nid] = {
                    **node,
                    "id":          nid,
                    "original_id": orig,
                    "floor":       floor_name,
                    "world":       [bpos[0]+node["x"], 0, bpos[2]+node["z"]],
                }
                self.graph[nid] = []

            for edge in data.get("edges", []):
                src = f"{floor_name}::{edge['source']}"
                tgt = f"{floor_name}::{edge['target']}"
                if src in self.nodes and tgt in self.nodes:
                    d = self._dist(self.nodes[src]["world"], self.nodes[tgt]["world"])
                    self.graph.setdefault(src, []).append((tgt, d))
                    self.graph.setdefault(tgt, []).append((src, d))
                    self.edges.append({**edge, "source": src, "target": tgt})

        # Cross-floor: connect staircases/elevators between adjacent floors
        verticals: dict[str, list[str]] = {}
        for nid, n in self.nodes.items():
            lbl = n.get("label", "").lower()
            if "stair" in lbl or "elevator" in lbl or "lift" in lbl:
                verticals.setdefault(n["floor"], []).append(nid)
        for f1, f2 in zip(sorted(verticals), sorted(verticals)[1:]):
            for n1 in verticals[f1]:
                for n2 in verticals[f2]:
                    # Only connect staircases/elevators that have the same label across floors
                    if self.nodes[n1].get("label", "").lower() == self.nodes[n2].get("label", "").lower():
                        self.graph.setdefault(n1, []).append((n2, FLOOR_CHANGE_COST))
                        self.graph.setdefault(n2, []).append((n1, FLOOR_CHANGE_COST))

        # Heal: connect isolated rooms to nearest waypoint
        # Pass 1 connects rooms to nearby waypoints. Pass 2 (component merge) is removed so it respects user-drawn paths strictly.
        self._heal()

        rooms = sum(1 for n in self.nodes.values() if n.get("type") != "waypoint")
        wpts  = sum(1 for n in self.nodes.values() if n.get("type") == "waypoint")
        print(f"✅ Graph ready: {rooms} rooms, {wpts} waypoints, {len(self.edges)} edges")

    # ── Auto-heal ──────────────────────────────────────────────────────────

    def _heal(self):
        floors = sorted({n["floor"] for n in self.nodes.values()})
        healed = 0
        for floor in floors:
            ids    = [nid for nid in self.nodes if nid.startswith(f"{floor}::")]
            id_set = set(ids)

            # Pass 1: rooms with 0 connections → connect to nearest waypoint
            for nid in ids:
                node = self.nodes[nid]
                if node.get("type") == "waypoint": continue
                if self.graph.get(nid): continue
                best_d, best_wp = float("inf"), None
                for wid in ids:
                    w = self.nodes[wid]
                    if w.get("type") != "waypoint": continue
                    dx = node["world"][0]-w["world"][0]
                    dz = node["world"][2]-w["world"][2]
                    d  = dx*dx + dz*dz
                    if node.get("building") != w.get("building"): d += 200
                    if d < best_d: best_d, best_wp = d, wid
                if best_wp:
                    dist = self._dist(self.nodes[nid]["world"], self.nodes[best_wp]["world"])
                    self.graph.setdefault(nid,     []).append((best_wp, dist))
                    self.graph.setdefault(best_wp, []).append((nid,     dist))
                    healed += 1

            # Pass 2 (component merge): Connect disconnected subgraphs
            components = []
            visited = set()
            for nid in ids:
                if nid in visited: continue
                comp = set()
                q = [nid]
                while q:
                    curr = q.pop(0)
                    if curr in comp: continue
                    comp.add(curr)
                    visited.add(curr)
                    for neighbor, _ in self.graph.get(curr, []):
                        if neighbor in id_set and neighbor not in comp:
                            q.append(neighbor)
                components.append(comp)
            
            if len(components) > 1:
                components.sort(key=len, reverse=True)
                main_comp = components[0]
                for comp in components[1:]:
                    best_d = float('inf')
                    best_pair = None
                    for n1 in comp:
                        if self.nodes[n1].get("type") != "waypoint": continue
                        for n2 in main_comp:
                            if self.nodes[n2].get("type") != "waypoint": continue
                            d = self._dist(self.nodes[n1]["world"], self.nodes[n2]["world"])
                            if d < best_d:
                                best_d = d
                                best_pair = (n1, n2)
                    
                    if not best_pair:
                        for n1 in comp:
                            for n2 in main_comp:
                                d = self._dist(self.nodes[n1]["world"], self.nodes[n2]["world"])
                                if d < best_d:
                                    best_d = d
                                    best_pair = (n1, n2)
                                    
                    if best_pair:
                        u, v = best_pair
                        self.graph.setdefault(u, []).append((v, best_d))
                        self.graph.setdefault(v, []).append((u, best_d))
                        main_comp.update(comp)
                        healed += 1


    # ── Utilities ──────────────────────────────────────────────────────────

    @staticmethod
    def _dist(a, b): return math.sqrt((a[0]-b[0])**2 + (a[2]-b[2])**2)

    @staticmethod
    def _angle(p1, p2, p3):
        v1x,v1z = p2[0]-p1[0], p2[2]-p1[2]
        v2x,v2z = p3[0]-p2[0], p3[2]-p2[2]
        cross = v1x*v2z - v1z*v2x
        dot   = v1x*v2x + v1z*v2z
        m1    = math.sqrt(v1x**2+v1z**2)
        m2    = math.sqrt(v2x**2+v2z**2)
        if m1 < 0.001 or m2 < 0.001: return 0.0
        return math.degrees(math.acos(max(-1, min(1, dot/(m1*m2))))) * (1 if cross > 0 else -1)

    # ── Room lookup ────────────────────────────────────────────────────────

    def find_room(self, query: str, floor: str = None) -> dict | None:
        q = query.lower().strip()
        cands = [n for n in self.nodes.values()
                 if n.get("type") != "waypoint"
                 and (floor is None or n.get("floor") == floor)]
        for n in cands:
            if n["label"].lower() == q: return n
        for n in cands:
            if q in n["label"].lower() or n["label"].lower() in q: return n
        best, best_r = None, 0.0
        for n in cands:
            r = SequenceMatcher(None, q, n["label"].lower()).ratio()
            if r > best_r and r > 0.5: best_r, best = r, n
        return best

    def list_rooms(self) -> list[str]:
        return sorted({n["label"] for n in self.nodes.values() if n.get("type") != "waypoint"})

    # ── Dijkstra ───────────────────────────────────────────────────────────

    def _dijkstra(self, start_id: str, end_id: str) -> list[str] | None:
        if start_id not in self.graph or end_id not in self.graph: return None
        dist = {start_id: 0.0}; prev = {start_id: None}
        heap = [(0.0, start_id)]; seen = set()
        while heap:
            d, u = heapq.heappop(heap)
            if u in seen: continue
            seen.add(u)
            if u == end_id:
                path, cur = [], end_id
                while cur: path.append(cur); cur = prev[cur]
                return path[::-1]
            for v, w in self.graph.get(u, []):
                alt = d + w
                if alt < dist.get(v, float("inf")):
                    dist[v] = alt; prev[v] = u
                    heapq.heappush(heap, (alt, v))
        return None

    # ── Directions ─────────────────────────────────────────────────────────

    def _directions(self, path_ids: list[str], dest_label: str) -> str:
        if len(path_ids) < 2: return f"You are already at {dest_label}."
        steps, prev_bld, prev_floor, cooldown = [], None, None, 0
        for i, nid in enumerate(path_ids):
            n = self.nodes[nid]
            bld, floor = n.get("building",""), n.get("floor","")
            if prev_bld and bld != prev_bld:
                b_old = self.buildings.get(prev_bld,{}).get("name", prev_bld)
                b_new = self.buildings.get(bld,{}).get("name", bld)
                steps.append(f"walk from {b_old} to {b_new}"); cooldown = 3
            prev_bld = bld
            if prev_floor and floor != prev_floor:
                fn = floor.replace("floor_",""); fo = prev_floor.replace("floor_","")
                verb = "up" if fn > fo else "down"
                via  = "elevator" if "elevator" in n.get("label","").lower() else "staircase"
                steps.append(f"take the {via} {verb} to Floor {fn}"); cooldown = 3
            prev_floor = floor
            if 0 < i < len(path_ids)-1 and cooldown <= 0:
                p1 = self.nodes[path_ids[i-1]]["world"]
                p2 = n["world"]
                p3 = self.nodes[path_ids[i+1]]["world"]
                ang = self._angle(p1, p2, p3)
                if abs(ang) > 38:
                    steps.append("turn left" if ang < 0 else "turn right"); cooldown = 2
            if cooldown > 0: cooldown -= 1
        steps.append(f"arrive at {dest_label}")
        return ", then ".join(steps).capitalize() + "."

    # ── Public API ─────────────────────────────────────────────────────────

    def find_path(self, destination: str, origin: str = None) -> dict | None:
        dest_node  = self.find_room(destination)
        if not dest_node:
            return {"error": f"Could not find '{destination}'. Try: {', '.join(self.list_rooms()[:8])}"}

        start_node = self.find_room(origin) if origin else self.find_room(self.ROBOT_LOCATION)
        if not start_node:
            # fallback: most-connected room
            start_node = max(
                (n for n in self.nodes.values() if n.get("type") != "waypoint"),
                key=lambda n: len(self.graph.get(n["id"], [])), default=None)
        if not start_node:
            return {"error": "Could not determine a starting location."}

        if start_node["id"] == dest_node["id"]:
            return {
                "destination": dest_node["label"], "floor": dest_node["floor"],
                "path_ids": [start_node["id"]], "path_coords": [dest_node["world"]],
                "directions": f"You are already at {dest_node['label']}.",
                "distance_m": 0, "time_min": 0,
                "nodes": list(self.nodes.values()), "buildings": self.buildings,
            }

        path_ids = self._dijkstra(start_node["id"], dest_node["id"])
        if not path_ids:
            return {"error": f"No connected path found to {dest_node['label']}. "
                             "Please check the map editor to ensure waypoints are connected."}

        coords     = [self.nodes[nid]["world"] for nid in path_ids]
        raw_dist   = sum(self._dist(coords[i-1], coords[i]) for i in range(1, len(coords)))
        dist_m     = round(raw_dist * 3, 1)
        time_min   = max(1, round(raw_dist * 3 / 1.4 / 60))
        directions = self._directions(path_ids, dest_node["label"])

        return {
            "destination":  dest_node["label"],
            "floor":        dest_node["floor"],
            "path_ids":     path_ids,
            "path_coords":  coords,
            "directions":   directions,
            "distance_m":   dist_m,
            "time_min":     time_min,
            "nodes":        list(self.nodes.values()),
            "buildings":    self.buildings,
        }


if __name__ == "__main__":
    wf = Wayfinder()
    print(f"Rooms: {len(wf.list_rooms())}")
    for dest in ["DEAN office", "Laboratory 7", "Lecture hall 1", "AI research laboratory"]:
        r = wf.find_path(dest)
        if r and "error" not in r:
            print(f"  ✅ {dest} → {r['floor']} {r['distance_m']}m {r['time_min']}min")
        else:
            print(f"  ❌ {dest}: {r}")
