from __future__ import annotations
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
    ROBOT_LOCATION = "You are here"

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
            print("[WARN] Wayfinder: no map files found in", self.data_dir)
            return
        for fp in files:
            name = Path(fp).stem.replace("map_graph_", "")
            with open(fp, encoding="utf-8") as f:
                self.floors[name] = json.load(f)
            n = len(self.floors[name].get("nodes", []))
            e = len(self.floors[name].get("edges", []))
            print(f"  [OK] Loaded {name} ({n} nodes, {e} edges)")
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
                clean_lbl = str(node.get("label", "")).strip()
                self.nodes[nid] = {
                    **node,
                    "label":       clean_lbl,
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

        # Register FIT EXPO 2026 stalls and 4th-floor robot station directly in memory
        # (no path calculation is needed; we only describe what is on this floor and other floors)
        for nid, n in list(self.nodes.items()):
            if n.get("label", "").strip().lower() == "you are here":
                n["floor"] = "floor_4"

        event_stalls = [
            ("floor_4::you_are_here", "You are here", "floor_4"),
            ("floor_4::level_1_projects", "Level 1 Projects", "floor_4"),
            ("floor_4::company_stalls", "Company Stalls", "floor_4"),
            ("floor_4::lunch_area", "Lunch Area", "floor_4"),
            ("floor_4::student_projects", "Student Projects", "floor_4"),
            ("floor_4::pg_research_lab", "PG Research Lab", "floor_4"),
            ("floor_4::hardware_lab", "Hardware Lab", "floor_4"),
            ("floor_4::ifs_network_lab", "IFS Network and Security Lab", "floor_4"),
            ("floor_4::behavioral_lab", "Behavioral Systems Lab", "floor_4"),
            ("floor_3::game_zone", "Game Zone", "floor_3"),
            ("floor_3::fyp_lab", "FYP Lab", "floor_3"),
            ("floor_2::software_projects", "Software Projects", "floor_2"),
            ("floor_2::research_projects", "Research Projects", "floor_2"),
            ("floor_1::photo_booth", "Photo Booth", "floor_1"),
            ("floor_1::historical_instrument", "Historical Instrument", "floor_1"),
            ("floor_1::level_2_4_projects", "Level 2 and 4 Projects", "floor_1"),
        ]
        for sid, label, fl in event_stalls:
            self.nodes[sid] = {
                "id": sid,
                "original_id": sid,
                "label": label,
                "type": "room",
                "floor": fl,
                "building": "building_2",
                "x": 0,
                "z": 0,
                "world": [0, 0, 0],
            }

        rooms = sum(1 for n in self.nodes.values() if n.get("type") != "waypoint")
        wpts  = sum(1 for n in self.nodes.values() if n.get("type") == "waypoint")
        print(f"[OK] Graph ready: {rooms} rooms, {wpts} waypoints, {len(self.edges)} edges")

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

    ROOM_ALIASES: dict[str, str] = {
        "lab 1": "Laboratory 1",
        "lab 01": "Laboratory 1",
        "lab 2": "labratory 2",
        "lab 02": "labratory 2",
        "laboratory 2": "labratory 2",
        "lab 3": "Laboratory 3",
        "lab 03": "Laboratory 3",
        "lab 4": "Laboratory 4",
        "lab 04": "Laboratory 4",
        "lab 5": "Laboratory 5",
        "lab 05": "Laboratory 5",
        "lab 6": "Laboratory 6",
        "lab 06": "Laboratory 6",
        "lab 7": "Laboratory 7",
        "lab 07": "Laboratory 7",
        "lab 8": "Laboratory 8",
        "lab 08": "Laboratory 8",
        "ai lab": "AI research laboratory",
        "ai laboratory": "AI research laboratory",
        "games": "Game Zone",
        "game stall": "Game Zone",
        "game stalls": "Game Zone",
        "gaming zone": "Game Zone",
        "software project": "Software Projects",
        "software stalls": "Software Projects",
        "research project": "Research Projects",
        "research stalls": "Research Projects",
        "level 2 and 4": "Software Projects",
        "company stall": "Company Stalls",
        "level 1 project": "Level 1 Projects",
        "student project": "Student Projects",
        "photo": "Photo Booth",
        "photobooth": "Photo Booth",
        "historical": "Historical Instrument",
        "lift": "Evaluator",
        "elevator": "Evaluator",
        "canteen": "Lunch Area",
        "food": "Lunch Area",
        "network lab": "IFS Network and Security Lab",
        "ifs lab": "IFS Network and Security Lab",
    }

    def find_room(self, query: str, floor: str = None) -> dict | None:
        q = query.lower().strip()
        if not q:
            return None
        if q in self.ROOM_ALIASES:
            q = self.ROOM_ALIASES[q].lower().strip()
        cands = [n for n in self.nodes.values()
                 if n.get("type") != "waypoint"
                 and (floor is None or n.get("floor") == floor)]
        # Prefer floor_4 when multiple floors have the same room label (e.g. Washrooms, Lift)
        cands.sort(key=lambda n: 0 if n.get("floor") == "floor_4" else 1)
        for n in cands:
            if n["label"].strip().lower() == q:
                return n
        for n in cands:
            lbl = n["label"].strip().lower()
            if q in lbl or lbl in q:
                return n
        best, best_r = None, 0.0
        for n in cands:
            lbl = n["label"].strip().lower()
            r = SequenceMatcher(None, q, lbl).ratio()
            if r > best_r and r > 0.5:
                best_r, best = r, n
        return best

    def find_rooms(self, query: str, floor: str = None, min_ratio: float = 0.65) -> list[dict]:
        """Return rooms that are genuinely ambiguous matches for *query*.

        Rules to prevent false buttons:
        1. If *query* matches a room label exactly (or with very high similarity >= 0.84
           to a single specific room), return only that 1 room (no disambiguation).
        2. If one or more rooms contain *query* as a substring (e.g. "auditorium" in
           "Auditorium 1", "Auditorium 2"), return ONLY those substring matches — never
           pad with unrelated fuzzy matches.
        3. Skip non-destination markers ("You are here", "EXIT", staircases) unless
           explicitly queried.
        """
        q = query.lower().strip()
        if not q:
            return []
        if q in self.ROOM_ALIASES:
            q = self.ROOM_ALIASES[q].lower().strip()

        skip_internal = {"you are here", "exit", "staircase 1", "staircase 2"}
        cands = [
            n for n in self.nodes.values()
            if n.get("type") != "waypoint"
            and (floor is None or n.get("floor") == floor)
            and (n["label"].strip().lower() not in skip_internal or q in n["label"].strip().lower())
        ]
        cands.sort(key=lambda n: 0 if n.get("floor") == "floor_4" else 1)

        # 1. Exact label match -> unambiguous single room
        for n in cands:
            if n["label"].strip().lower() == q:
                return [{**n, "label": n["label"].strip()}]

        # 2. Substring matches (e.g. "auditorium" -> Auditorium 1, Auditorium 2)
        seen_labels: set[str] = set()
        substring_hits: list[dict] = []
        for n in cands:
            label_clean = n["label"].strip()
            label_lc = label_clean.lower()
            if label_lc in seen_labels:
                continue
            if q in label_lc:
                seen_labels.add(label_lc)
                substring_hits.append({**n, "label": label_clean})

        if substring_hits:
            substring_hits.sort(key=lambda item: item["label"])
            return substring_hits[:4]

        # 3. High-confidence single typo match (e.g. "labratory 1" -> "Laboratory 1")
        scored: list[tuple[float, dict]] = []
        for n in cands:
            label_clean = n["label"].strip()
            label_lc = label_clean.lower()
            if label_lc in seen_labels:
                continue
            score = SequenceMatcher(None, q, label_lc).ratio()
            if score >= min_ratio:
                seen_labels.add(label_lc)
                scored.append((score, {**n, "label": label_clean}))

        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.84:
            import re as _re
            top_lbl = scored[0][1]["label"].lower()
            missing_num = bool(_re.search(r"\d", top_lbl)) and not bool(_re.search(r"\d", q))
            missing_gender = bool(_re.search(r"\b(male|female)\b", top_lbl)) and not bool(_re.search(r"\b(male|female)\b", q))
            if not missing_num and not missing_gender:
                if len(scored) == 1 or (scored[0][0] - scored[1][0]) >= 0.08:
                    return [scored[0][1]]

        return [n for _, n in scored[:4]]

    def list_rooms(self) -> list[str]:
        return sorted({n["label"].strip() for n in self.nodes.values() if n.get("type") != "waypoint" and n.get("label", "").strip()})

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

    _CUSTOM_TEXT_DIRECTIONS: dict[str, str] = {
        # 4th Floor (Same floor as robot)
        "level 1 projects": "You are on the 4th floor! Level 1 Projects are in this hall and in the Auditorium on this floor.",
        "company stalls": "You are on the 4th floor! Company Stalls are right outside in the Lunch Area on this floor.",
        "lunch area": "You are on the 4th floor! The Lunch Area is right outside along the corridor on this floor.",
        "study area": "You are on the 4th floor! The Lunch and Study Area is right outside on this floor.",
        "student projects": "You are on the 4th floor! Student Projects are in the adjacent hall between Lab 6 and PG Research Lab.",
        "auditorium 2": "You are on the 4th floor! The Auditorium is in the other hall on this floor, near the lift.",
        "auditorium 1": "You are right here in this 4th floor exhibition hall! The main Auditorium is also on this floor near the lift.",
        "ai research laboratory": "You are on the 4th floor! The AI Lab is right next to Lab 6 on this floor.",
        "laboratory 6": "You are on the 4th floor! Lab 6 is right next to this hall on this floor.",
        "pg research lab": "You are on the 4th floor! The PG Research Lab is in the hall past Lab 6 on this floor.",
        "server room": "You are on the 4th floor! The Server Room is next to the PG Research Lab on this floor.",
        "hardware lab": "You are on the 4th floor! The Hardware Lab is right next to the Lunch Area on this floor.",
        "de laboratory": "You are on the 4th floor! The Hardware and DE Lab is next to the Lunch Area on this floor.",
        "ifs network and security lab": "You are on the 4th floor! The IFS Network and Security Lab is right next to the Lunch Area on this floor.",
        "embedded system laboratory": "You are on the 4th floor! The Electronic and Embedded Systems Lab is on this floor near the lift.",
        "microcontroller based system": "You are on the 4th floor! The Embedded Systems Lab is along the corridor on this floor.",
        "audio visual unit extension": "You are on the 4th floor! The Audio Visual Unit is along the corridor on this floor.",
        "behavioral systems lab": "You are on the 4th floor! The Behavioral Systems Lab is on this floor next to the lift.",
        "evaluator": "You are on the 4th floor! The lift is on this floor near the Auditorium.",
        "male washroom": "You are on the 4th floor! The Male Washroom is along the corridor on this floor.",
        "female washroom": "You are on the 4th floor! The Female Washroom is along the corridor on this floor.",
        # 3rd Floor (1 floor down from 4th floor)
        "game zone": "You are on the 4th floor. Go down one floor to the 3rd floor for the Game Zone halls near Lab 3.",
        "fyp lab": "You are on the 4th floor. Go down one floor to the 3rd floor for the FYP Lab and Lab 3.",
        "laboratory 4": "You are on the 4th floor. Go down one floor to the 3rd floor for the FYP Lab.",
        "laboratory 3": "You are on the 4th floor. Go down one floor to the 3rd floor for Lab 3.",
        "department of interdisciplinary studies": "You are on the 4th floor. Go down one floor to the 3rd floor for the Department of Interdisciplinary Studies.",
        "laboratory 8": "You are on the 4th floor. Go down one floor to the 3rd floor for Lab 8 near the lift.",
        # 2nd Floor (2 floors down from 4th floor — Software & Research Projects)
        "software projects": "You are on the 4th floor. Go down two floors to the 2nd floor for the Software and Research Projects halls.",
        "research projects": "You are on the 4th floor. Go down two floors to the 2nd floor for the Research and Software Projects halls.",
        "department of computational mathematics": "You are on the 4th floor. Go down two floors to the 2nd floor for the Department of Computational Mathematics.",
        "laboratory 5": "You are on the 4th floor. Go down two floors to the 2nd floor for Lab 5 and Lab 2.",
        "labratory 2": "You are on the 4th floor. Go down two floors to the 2nd floor for Lab 2 and Lab 5.",
        # 1st Floor (3 floors down from 4th floor)
        "photo booth": "You are on the 4th floor. Go down to the 1st floor for the Photo Booth hall near Lab 1.",
        "historical instrument": "You are on the 4th floor. Go down to the 1st floor for the Historical Instrument hall near the lift.",
        "level 2 and 4 projects": "You are on the 4th floor. Go down to the 1st floor for Level 2 and 4 Projects near Lab 1.",
        "dean's office": "You are on the 4th floor. Go down to the 1st floor for the Dean's Office next to Lab 1.",
        "laboratory 1": "You are on the 4th floor. Go down to the 1st floor for Lab 1 next to the Dean's Office.",
        "department of information technology": "You are on the 4th floor. Go down to the 1st floor for the Department of Information Technology.",
        "exit": "You are on the 4th floor. Take the lift or stairs down to the 1st floor to exit the building.",
    }

    def _directions_for_node(self, dest_node: dict) -> str:
        clean_dest = dest_node["label"].strip()
        key = clean_dest.lower()
        if key in self._CUSTOM_TEXT_DIRECTIONS:
            return self._CUSTOM_TEXT_DIRECTIONS[key]

        dest_floor = dest_node.get("floor", "floor_4")
        floor_num = int(dest_floor.replace("floor_", ""))

        floor_map = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
        floor_str = floor_map.get(floor_num, f"{floor_num}th")

        if dest_floor == "floor_4":
            return f"You are on the 4th floor! {clean_dest} is in one of the halls on this floor."

        diff = 4 - floor_num
        if diff == 1:
            step_phrase = "down one floor"
        elif diff > 1:
            step_phrase = f"down {diff} floors"
        else:
            step_phrase = f"to the {floor_str} floor"
        return f"You are on the 4th floor. Go {step_phrase} to the {floor_str} floor for {clean_dest}."

    # ── Public API ─────────────────────────────────────────────────────────

    def find_path(self, destination: str, origin: str = None) -> dict | None:
        """Look up the floor and hall description for *destination* without path calculation."""
        dest_node = self.find_room(destination)
        if not dest_node:
            return {"error": f"Could not find '{destination}'."}

        directions = self._directions_for_node(dest_node)

        return {
            "destination":  dest_node["label"],
            "floor":        dest_node.get("floor", "floor_4"),
            "path_ids":     [],
            "path_coords":  [],
            "directions":   directions,
            "distance_m":   0,
            "time_min":     0,
            "nodes":        [],
            "buildings":    {},
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
